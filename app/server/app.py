"""FastAPI app: pages, partials, JSON, SSE, and the localhost guards (DESIGN §13, §15)."""

from __future__ import annotations

import asyncio
import json
import hashlib
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core import accents, db, paths, projectors
from app.core.normalize import mask_fid, normalize_symbol
from app.core.refdata.loader import status as refdata_status
from app.core import builds as builds_core
from app.core import fits as fits_core
from app.core import engplan
from app.core import goals as goals_core
from app.core import notes as notes_core
from app.core.backup import archive as backup_archive
from app.core.backup import restore as backup_restore
from app.server import activity_views, build_views, carrier_views, engineer_views, engplan_views, fit_views, goal_views, history_views, live_views, queries, views
from app.server.context import AppContext

TOKEN_HEADER = "X-Planner-Token"
KEEPALIVE_SECONDS = 15


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    path: str
    phase: int | None
    summary: str


TABS = [
    Tab("dashboard", "Dashboard", "/", None,
        "Live view: current ship, balance, Merc Coins, rank strip, session summary."),
    Tab("overview", "Overview", "/progress", None,
        "Progress, Statistics, History and Ledger."),
    Tab("ships", "Ships", "/ships", None,
        "Per-ship current fit with engineering per slot and before/after stats."),
    Tab("materials", "Materials", "/materials", None,
        "Current counts against caps, by type, family and grade."),
    Tab("activities", "Activities", "/activities", None,
        "Trading, mining, exploration and exobiology: session and lifetime stats, rates, favourites and kit."),
    Tab("goals", "Goals", "/goals", None,
        "Goals carried over from the Progression Planner workbook, with material, credit and Merc Coin requirements."),
    Tab("notes", "Notes", "/notes", None,
        "Your own notes, per commander, kept in the data folder."),
    Tab("settings", "Settings", "/settings", None, ""),
]
# The Overview tab's subtabs, in order; the tab itself opens the first (DESIGN §13).
OVERVIEW_TABS = [
    Tab("progress", "Progress", "/progress", None,
        "Pilots' Federation ranks, superpower reputation, navy ranks and engineer grades as bars, with a Promotion available tag."),
    Tab("statistics", "Statistics", "/statistics", None,
        "Every lifetime counter from Statistics, grouped by category, with change over any date range and sparklines."),
    Tab("history", "History", "/history", None,
        "Calendar heatmap of play days and line charts for balance, wealth, Merc Coins, rank progress and materials."),
    Tab("ledger", "Ledger", "/ledger", None,
        "Credits by category with itemized events and an open Unattributed share; Merc Coins, ships, modules, unlocks."),
]
TABS_BY_KEY = {t.key: t for t in TABS + OVERVIEW_TABS}


def _localtime(ts: str | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    return dt.astimezone().strftime(fmt)


def _num(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    return f"{int(value):,}"


def _unit(value, unit: str, signed: bool = False) -> str:
    """Format a Statistics value by its stats_map.csv unit."""
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else "−" if signed and value < 0 else ""
    v = abs(value) if signed else value
    if unit == "credits":
        text = f"{v:,.0f} cr"
    elif unit == "merc_coins":
        text = f"{v:,.0f} MC"
    elif unit == "seconds":
        hours, rem = divmod(int(v), 3600)
        text = f"{hours:,}h {rem // 60:02d}m"
    elif unit == "ly":
        text = f"{v:,.1f} ly"
    elif unit == "metres":
        text = f"{v / 1000:,.1f} km" if v >= 1000 else f"{v:,.0f} m"
    elif unit == "ratio":
        text = f"{v:,.2f}"
    else:
        text = _num(v)
    return sign + text


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        ctx.broadcaster.bind(asyncio.get_running_loop())
        yield

    app = FastAPI(title="Elite Dangerous Commander Planner", version=ctx.version, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    resources = paths.resource_dir() / "app" / "server"
    templates = Jinja2Templates(directory=resources / "templates")
    templates.env.filters["localtime"] = _localtime
    templates.env.filters["num"] = _num
    templates.env.filters["mask"] = mask_fid
    templates.env.filters["unit"] = _unit
    # Cache-bust static assets by content, so an updated build never runs with a stale stylesheet.
    digest = hashlib.sha256()
    for asset in sorted((resources / "static").glob("*")):
        digest.update(asset.read_bytes())
    templates.env.globals["asset_version"] = digest.hexdigest()[:10]
    app.mount("/static", StaticFiles(directory=resources / "static"), name="static")

    allowed_hosts = {f"127.0.0.1:{ctx.port}", f"localhost:{ctx.port}"}

    @app.middleware("http")
    async def host_header_check(request: Request, call_next):
        # Blocks DNS rebinding: only our own loopback origin may talk to us.
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return PlainTextResponse("Host not allowed", status_code=400)
        return await call_next(request)

    def require_token(request: Request) -> None:
        sent = request.headers.get(TOKEN_HEADER, "")
        if not secrets.compare_digest(sent, ctx.token):
            raise HTTPException(status_code=403, detail="missing or invalid token")

    def conn():
        c = db.connect(ctx.db_path, any_thread=True)
        try:
            yield c
        finally:
            c.close()

    def page(request: Request, c, template: str, active: str, **extra) -> HTMLResponse:
        fid = queries.selected_fid(c)
        context = {
            "tabs": TABS,
            "overview_tabs": OVERVIEW_TABS,
            "active": active,
            "active_top": "overview" if active in {t.key for t in OVERVIEW_TABS} else active,
            "active_label": TABS_BY_KEY[active].label if active in TABS_BY_KEY else "",
            "token": ctx.token,
            "version": ctx.version,
            "commanders": queries.commanders(c),
            "fid": fid,
            "import_status": ctx.import_status.snapshot(),
            "unknown_count": queries.unknown_symbol_count(c),
            "last_import": queries.last_completed_import(c),
            "game": live_views.game_state(c, fid, ctx.live.snapshot()["status"]),
            "hover_cards": db.get_setting(c, "hover_cards") != "off",
            "accent": accents.get(db.get_setting(c, "accent")),
            "accents": accents.ACCENTS.values(),
            **extra,
        }
        return templates.TemplateResponse(request, template, context)

    # -- pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return page(
            request, c, "dashboard.html", "dashboard",
            login=queries.last_login(c, fid) if fid else None,
            totals=queries.event_totals(c, fid) if fid else None,
            activity=queries.activity_by_day(c, fid) if fid else [],
            strip=views.rank_strip(c, fid) if fid else None,
            live=live_views.live_view(c, fid, ctx.live.snapshot()["status"]) if fid else None,
            plan=plan_for(c, fid) if fid else None,
        )

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request, range: str = "90d", track: str = "combat", material: str = "",
                     c=Depends(conn)):
        fid = queries.selected_fid(c)
        return page(request, c, "history.html", "history",
                    h=history_views.history_view(c, fid, range, track, material or None) if fid else None)

    @app.get("/ledger", response_class=HTMLResponse)
    def ledger_page(request: Request, tab: str = "credits", range: str = "session", change: str = "",
                    c=Depends(conn)):
        fid = queries.selected_fid(c)
        sub = tab if tab in ("credits", "merc", "ships", "modules", "unlocks") else "credits"
        values = {}
        if fid:
            if sub == "credits":
                values["credits"] = history_views.ledger_credits_view(c, fid, range)
            elif sub == "merc":
                values["merc"] = history_views.ledger_merc_view(c, fid)
            elif sub == "ships":
                values["ships"] = history_views.ships_list(c, fid)
            elif sub == "modules":
                values["modules"] = history_views.module_history_view(c, fid, change or None)
                values["change"] = change
            else:
                values["unlocks"] = history_views.unlocks_view(c, fid)
        return page(request, c, "ledger.html", "ledger", sub=sub, **values)

    @app.get("/ships", response_class=HTMLResponse)
    def ships_page(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        fits = history_views.ship_fits(c, fid) if fid else []
        money = {f["ship_id"]: f for f in goal_views.fleet(c, fid)} if fid else {}
        fctx = fit_views.context(c, fid) if fid else None
        for s in fits:
            s["saved_fits"] = fit_views.ship_fits(c, fid, s["ship_id"], fctx, ctx.version)
            s["value"], s["rebuy"] = money.get(s["ship_id"], {}).get("value"), money.get(s["ship_id"], {}).get("rebuy")
            try:
                s["share"] = builds_core.share(builds_core.ship_loadout(c, fid, s["ship_id"]), ctx.version)
            except LookupError:
                s["share"] = None
        return page(request, c, "ships.html", "ships", fits=fits, storage_as_of=fctx["storage"]["as_of"] if fctx else None,
                    carrier=carrier_views.carrier_view(c, fid) if fid else None,
                    bview=build_views.builds_view(c, fid) if fid else None)

    @app.get("/partials/builds", response_class=HTMLResponse)
    def builds_partial(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/builds.html", bview=build_views.builds_view(c, fid) if fid else None)

    def plan_for(c, fid):
        live = live_views.live_view(c, fid, ctx.live.snapshot()["status"])
        plan = goal_views.plan_view(c, fid, live["balance"]["value"] if live else None, ctx.journal_dir / "Cargo.json")
        plan["engineer_next"] = engineer_views.chain_view(c, fid)["next"]
        return plan

    def goal_form_options(c):
        ships = [(r[0], r[1]) for r in c.execute(
            "SELECT symbol, display_name FROM ref_items WHERE kind = 'ship' ORDER BY display_name")]
        tracks = [(k, label, views.rank_names().get(k, [])) for k, label in views.PILOT_TRACKS + views.NAVY_TRACKS]
        builds = [(r[0], r[1], r[2]) for r in c.execute(
            "SELECT id, name, ship_type FROM builds WHERE fid = ? ORDER BY id", (queries.selected_fid(c),))]
        return {"ship_options": ships, "track_options": tracks, "build_options": builds,
                "flag_options": [(k, d.label) for k, d in goal_views.flag_defs().items()]}

    @app.get("/goals", response_class=HTMLResponse)
    def goals_page(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return page(request, c, "goals.html", "goals", plan=plan_for(c, fid) if fid else None,
                    chain=engineer_views.chain_view(c, fid) if fid else None,
                    eplan=engplan_views.plan_view(c, fid) if fid else None,
                    **goal_form_options(c), **engplan_views.form_options(c))

    @app.get("/partials/goals", response_class=HTMLResponse)
    def goals_partial(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/goals.html", plan=plan_for(c, fid) if fid else None,
                       token=ctx.token, **goal_form_options(c))

    @app.get("/partials/engineer-plan", response_class=HTMLResponse)
    def engineer_plan_partial(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/engineer_plan.html",
                       chain=engineer_views.chain_view(c, fid) if fid else None)

    def activities_for(c, fid, tab, which):
        tab = tab if tab in activity_views.ACTIVITIES else activity_views.default_tab(c, fid)
        view = getattr(activity_views, f"{tab}_view")(c, fid, which or "session")
        return {"atab": tab, "which": which or "session", "av": view,
                "life": activity_views.lifetime(c, fid, tab), "first_journal": activity_views.first_journal(c, fid),
                "unlocks": activity_views.unlock_items(c, fid, ctx.journal_dir / "Cargo.json").get(tab, []),
                "ship_opts": activity_views.ship_options(c, fid)}

    @app.get("/activities", response_class=HTMLResponse)
    def activities_page(request: Request, tab: str = "", s: str = "", c=Depends(conn)):
        fid = queries.selected_fid(c)
        return page(request, c, "activities.html", "activities", **(activities_for(c, fid, tab, s) if fid else {"av": None}))

    @app.get("/partials/activities", response_class=HTMLResponse)
    def activities_partial(request: Request, tab: str = "", s: str = "", c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/activities.html", **(activities_for(c, fid, tab, s) if fid else {"av": None}))

    @app.get("/partials/eng-plan", response_class=HTMLResponse)
    def eng_plan_partial(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/eng_plan.html",
                       eplan=engplan_views.plan_view(c, fid) if fid else None, **engplan_views.form_options(c))

    @app.get("/partials/nextup", response_class=HTMLResponse)
    def nextup_partial(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/nextup.html", plan=plan_for(c, fid) if fid else None)

    @app.get("/materials", response_class=HTMLResponse)
    def materials_page(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return page(request, c, "materials.html", "materials", **mats_for(c, fid))

    def mats_for(c, fid):
        if not fid:
            return {"mats": None, "eplan": None}
        mats = live_views.materials_view(c, fid)
        eplan = engplan_views.plan_view(c, fid)
        need = {x["symbol"]: x for x in eplan["materials"]}
        for families in mats["groups"].values():
            for _family, rows in families:
                for m in rows:
                    m["plan"] = need.get(m["symbol"])
        return {"mats": mats, "eplan": eplan}

    @app.get("/progress", response_class=HTMLResponse)
    def progress_page(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return page(request, c, "progress.html", "progress", view=progress_for(c, fid) if fid else None)

    def progress_for(c, fid):
        view = views.progress_view(c, fid)
        view["eng_steps"] = {e["id"]: e["next_step"] for e in engineer_views.chain_view(c, fid)["all"]}
        return view

    @app.get("/notes", response_class=HTMLResponse)
    def notes_page(request: Request, n: str = "", q: str = "", edit: str = "", c=Depends(conn)):
        fid = queries.selected_fid(c)
        if not fid:
            return page(request, c, "notes.html", "notes", notes=None)
        listed = notes_core.list_notes(ctx.data_dir, fid, q)
        note = None
        if n:
            try:
                note = notes_core.get(ctx.data_dir, fid, n)
            except LookupError:
                note = None
        elif listed:
            note = listed[0]
        for x in listed:
            x["excerpt"] = notes_core.excerpt(x["body"])
        return page(request, c, "notes.html", "notes", notes=listed, note=note, q=q,
                    editing=bool(edit and note), rendered=notes_core.render(note["body"], tasks=True) if note else "",
                    data_dir=ctx.data_dir, total=len(notes_core.list_notes(ctx.data_dir, fid)))

    @app.get("/statistics", response_class=HTMLResponse)
    def statistics_page(request: Request, range: str = "session", q: str = "", c=Depends(conn)):
        fid = queries.selected_fid(c)
        return page(request, c, "statistics.html", "statistics",
                    view=views.overview_view(c, fid, range) if fid else None, q=q[:100])

    @app.get("/overview")
    def overview_redirect(range: str = ""):
        # Before 5f the Statistics page lived here; a range means an old Statistics link.
        return RedirectResponse(f"/statistics?range={range}" if range else "/progress", status_code=307)

    def placeholder_route(tab: Tab):
        def view(request: Request, c=Depends(conn)):
            return page(request, c, "placeholder.html", tab.key, tab=tab)
        view.__name__ = f"tab_{tab.key}"
        return view

    for tab in TABS:
        if tab.phase is not None:
            app.get(tab.path, response_class=HTMLResponse)(placeholder_route(tab))

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, c=Depends(conn)):
        return page(
            request, c, "settings.html", "settings",
            journal_dir=ctx.journal_dir, journal_dir_exists=ctx.journal_dir.is_dir(),
            data_dir=ctx.data_dir, port=ctx.port,
            refdata=ctx.refdata or refdata_status(c, paths.refdata_dir()),
        )

    @app.get("/settings/backup", response_class=HTMLResponse)
    def backup_page(request: Request, saved: str = "", done: int = 0, c=Depends(conn)):
        imports = queries.edplanner_imports(c)
        target = backup_restore.rollback_target(c, ctx.data_dir)
        return page(
            request, c, "backup.html", "settings",
            backups=backup_archive.list_backups(ctx.data_dir), backups_dir=backup_archive.backups_dir(ctx.data_dir),
            keep=backup_archive.KEEP, saved=saved, imports=imports,
            done=next((r for r in imports if r["id"] == done), None),
            rollback_id=target["id"] if target else None,
        )

    @app.get("/settings/backup/review", response_class=HTMLResponse)
    def backup_review(request: Request, file: str = "", c=Depends(conn)):
        try:
            path = backup_archive.resolve(ctx.data_dir, file)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such file")
        arc, review, error = None, None, None
        try:
            arc = backup_archive.Archive(path, backup_archive.display_name(file))
            review = backup_restore.review(c, ctx.data_dir, arc)
        except ValueError as exc:
            error = str(exc)
        return page(request, c, "backup_review.html", "settings", ref=file, name=backup_archive.display_name(file),
                    manifest=arc.manifest if arc else None, review=review, error=error,
                    kinds=backup_archive.KINDS, modes=backup_restore.MODES)

    @app.get("/settings/diagnostics", response_class=HTMLResponse)
    def diagnostics_page(request: Request, c=Depends(conn)):
        return page(
            request, c, "diagnostics.html", "settings",
            log_rows=queries.import_log(c),
            dropped=queries.dropped_by_type(c),
            provisional=queries.provisional(c),
            stored_types=queries.event_types(c, None),
            handled_events=set(projectors.EVENTS),
            live=ctx.live.snapshot(),
            logs_dir=ctx.logs_dir,
        )

    # -- partials (re-fetched on SSE messages) ----------------------------

    @app.get("/partials/import", response_class=HTMLResponse)
    def import_partial(request: Request, c=Depends(conn)):
        return templates.TemplateResponse(request, "partials/import.html", {
            "import_status": ctx.import_status.snapshot(),
            "last_import": queries.last_completed_import(c),
            "token": ctx.token,
        })

    def partial(request: Request, c, template: str, **values) -> HTMLResponse:
        values.setdefault("hover_cards", db.get_setting(c, "hover_cards") != "off")
        return templates.TemplateResponse(request, template, values)

    @app.get("/partials/live", response_class=HTMLResponse)
    def live_partial(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/live.html",
                       live=live_views.live_view(c, fid, ctx.live.snapshot()["status"]) if fid else None)

    @app.get("/partials/gamestate", response_class=HTMLResponse)
    def gamestate_partial(request: Request, c=Depends(conn)):
        return partial(request, c, "partials/gamestate.html",
                       game=live_views.game_state(c, queries.selected_fid(c), ctx.live.snapshot()["status"]))

    @app.get("/partials/strip", response_class=HTMLResponse)
    def strip_partial(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/strip.html", strip=views.rank_strip(c, fid) if fid else None)

    @app.get("/partials/progress", response_class=HTMLResponse)
    def progress_partial(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/progress.html", view=progress_for(c, fid) if fid else None)

    @app.get("/partials/materials", response_class=HTMLResponse)
    def materials_partial(request: Request, c=Depends(conn)):
        fid = queries.selected_fid(c)
        return partial(request, c, "partials/materials.html", **mats_for(c, fid))

    @app.get("/api/live")
    def live_status():
        return ctx.live.snapshot()

    # -- JSON / actions ---------------------------------------------------

    @app.get("/api/import/status")
    def import_status():
        return ctx.import_status.snapshot()

    @app.post("/api/rescan", dependencies=[Depends(require_token)])
    def rescan():
        started = ctx.start_rescan()
        return JSONResponse({"started": started}, status_code=202 if started else 409)

    @app.post("/api/rebuild", dependencies=[Depends(require_token)])
    def rebuild_derived():
        c = db.connect(ctx.db_path)
        try:
            counts = ctx.projection.rebuild(c)
        finally:
            c.close()
        for area in ("progress", "materials", "live"):
            ctx.broadcaster.publish(area)
        return counts

    # -- goals (Phase 5a) ------------------------------------------------------

    def _goal_conn():
        c = db.connect(ctx.db_path, any_thread=True)
        fid = queries.selected_fid(c)
        if not fid:
            c.close()
            raise HTTPException(status_code=409, detail="no commander yet")
        return c, fid

    def _own_goal(c, fid, goal_id):
        row = c.execute("SELECT * FROM goals WHERE id = ? AND fid = ?", (goal_id, fid)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no such goal")
        return row

    def _goals_changed():
        ctx.broadcaster.publish("goals")

    def _bad(exc: ValueError):
        confirm = getattr(exc, "confirm", None)     # the user can say yes and send it again with retarget
        if confirm:
            return JSONResponse({"error": str(exc), "confirm": confirm}, status_code=409)
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/goals/load-template", dependencies=[Depends(require_token)])
    def goals_load_template():
        c, fid = _goal_conn()
        try:
            added = goals_core.load_template_goals(c, fid, goal_views.template())
        finally:
            c.close()
        _goals_changed()
        return {"added": added}

    @app.post("/api/goals", dependencies=[Depends(require_token)])
    async def goals_create(request: Request):
        payload = await request.json()
        c, fid = _goal_conn()
        try:
            cols = goals_core.columns_from_form(payload)
            cols["priority"] = c.execute("SELECT COALESCE(MAX(priority), 0) + 1 FROM goals WHERE fid = ?",
                                         (fid,)).fetchone()[0]
            with c:
                goal_id = goals_core.insert_goal(c, fid, cols)
        except ValueError as exc:
            return _bad(exc)
        finally:
            c.close()
        _goals_changed()
        return {"id": goal_id}

    @app.post("/api/goals/{goal_id}/update", dependencies=[Depends(require_token)])
    async def goals_update(goal_id: int, request: Request):
        payload = await request.json()
        c, fid = _goal_conn()
        try:
            row = _own_goal(c, fid, goal_id)
            goals_core.update_goal(c, goal_id, goals_core.columns_from_form(payload, row))
        except ValueError as exc:
            return _bad(exc)
        finally:
            c.close()
        _goals_changed()
        return {"ok": True}

    @app.post("/api/goals/{goal_id}/{action}", dependencies=[Depends(require_token)])
    async def goals_action(goal_id: int, action: str, request: Request):
        payload = await request.json() if (await request.body()) else {}
        c, fid = _goal_conn()
        try:
            _own_goal(c, fid, goal_id)
            if action == "status":
                goals_core.set_status(c, goal_id, str(payload.get("status")))
            elif action == "move":
                goals_core.move(c, fid, goal_id, int(payload.get("delta", 0)))
            elif action == "step":
                goals_core.toggle_step(c, goal_id, str(payload.get("step")), bool(payload.get("done")))
            elif action == "delete":
                goals_core.delete_goal(c, fid, goal_id)
            elif action == "build":
                pick = str(payload.get("build_id") or "")
                builds_core.attach(c, fid, goal_id, str(payload.get("text") or ""), int(pick) if pick.isdigit() else None,
                                   retarget=bool(payload.get("retarget")))
            else:
                raise HTTPException(status_code=404, detail="unknown action")
        except ValueError as exc:
            return _bad(exc)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        finally:
            c.close()
        _goals_changed()
        return {"ok": True}

    @app.post("/api/flags", dependencies=[Depends(require_token)])
    async def flags_set(request: Request):
        payload = await request.json()
        c, fid = _goal_conn()
        try:
            if payload.get("key") not in goal_views.flag_defs():
                raise ValueError("unknown flag")
            goals_core.set_flag(c, fid, payload["key"], str(payload.get("value")))
        except ValueError as exc:
            return _bad(exc)
        finally:
            c.close()
        _goals_changed()
        return {"ok": True}

    @app.post("/api/engineers/{engineer_id}/pin", dependencies=[Depends(require_token)])
    async def engineer_pin(engineer_id: int, request: Request):
        payload = await request.json()
        c, fid = _goal_conn()
        try:
            pick = str(payload.get("pin") or "")
            if pick:
                module_type, _, blueprint = pick.partition("|")
                engineer_views.set_pin(c, fid, engineer_id, module_type, blueprint)
            else:
                engineer_views.set_pin(c, fid, engineer_id, None, None)
        except ValueError as exc:
            return _bad(exc)
        finally:
            c.close()
        _goals_changed()
        return {"ok": True}

    def _engplan_call(fn):
        c, fid = _goal_conn()
        try:
            result = fn(c, fid)
        except ValueError as exc:
            return _bad(exc)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        finally:
            c.close()
        _goals_changed()
        return result

    @app.post("/api/engplan/load-template", dependencies=[Depends(require_token)])
    def engplan_load_template():
        return _engplan_call(lambda c, fid: {"added": engplan.load_template(c, fid, engplan_views.template())})

    @app.post("/api/engplan/rolls", dependencies=[Depends(require_token)])
    async def engplan_rolls(request: Request):
        payload = await request.json()

        def save(c, fid):
            r = engplan.parse_rolls(str(payload.get("rolls") or ""))
            with c:
                db.set_setting(c, engplan.ROLLS_SETTING, ",".join(map(str, r)))
            return {"rolls": r}
        return _engplan_call(save)

    @app.post("/api/engplan", dependencies=[Depends(require_token)])
    async def engplan_create(request: Request):
        payload = await request.json()
        module_type, _, blueprint = str(payload.pop("pick", "")).partition("|")
        return _engplan_call(lambda c, fid: {"id": engplan.add(c, fid, payload | {
            "module_type": module_type, "blueprint": blueprint})})

    @app.post("/api/engplan/{entry_id}/{action}", dependencies=[Depends(require_token)])
    async def engplan_action(entry_id: int, action: str, request: Request):
        payload = await request.json() if (await request.body()) else {}

        def act(c, fid):
            if action == "update":
                engplan.update(c, fid, entry_id, {k: v for k, v in payload.items() if k in engplan.FIELDS})
            elif action == "include":
                engplan.update(c, fid, entry_id, {"include": bool(payload.get("include"))})
            elif action == "move":
                engplan.move(c, fid, entry_id, int(payload.get("delta", 0)))
            elif action == "delete":
                engplan.delete(c, fid, entry_id)
            else:
                raise LookupError("unknown action")
            return {"ok": True}
        return _engplan_call(act)

    @app.post("/api/builds", dependencies=[Depends(require_token)])
    async def builds_import(request: Request):
        payload = await request.json()

        def imp(c, fid):
            parsed = builds_core.parse(str(payload.get("text") or ""))
            # A build for a ship you own becomes another fit on that ship; any other stays a build.
            ship_id = builds_core.default_ship(c, fid, normalize_symbol(parsed["loadout"]["Ship"]))
            if ship_id is None:
                return {"id": builds_core.add(c, fid, parsed, str(payload.get("name") or ""))}
            name = str(payload.get("name") or "").strip() or parsed.get("name") or "Imported"
            fit_id = fits_core.add(c, fid, ship_id, parsed["loadout"], name, unique_name=True)
            return {"fit_id": fit_id, "ship_id": ship_id, "redirect": f"/ships?fit={fit_id}"}
        return _engplan_call(imp)

    @app.post("/api/builds/example", dependencies=[Depends(require_token)])
    def builds_example():
        return _engplan_call(lambda c, fid: {"id": builds_core.load_template(
            c, fid, build_views.EXAMPLE_KEY, build_views.example_text())})

    @app.post("/api/builds/from-ship", dependencies=[Depends(require_token)])
    async def builds_from_ship(request: Request):
        payload = await request.json()
        return _engplan_call(lambda c, fid: {"id": builds_core.from_ship(c, fid, int(payload.get("ship_id", -1)))})

    @app.get("/api/settings/display")
    def settings_display_get(c=Depends(conn)):
        """The display settings, for tabs that were open when they changed."""
        a = accents.get(db.get_setting(c, "accent"))
        return {"accent": a.key, "hex": a.hex, "live": a.live, "hover_cards": db.get_setting(c, "hover_cards") != "off"}

    @app.post("/api/settings/display", dependencies=[Depends(require_token)])
    async def settings_display(request: Request):
        payload = await request.json()
        c = db.connect(ctx.db_path)
        try:
            if "hover_cards" in payload:
                db.set_setting(c, "hover_cards", "on" if payload.get("hover_cards") else "off")
            if "accent" in payload:
                if payload["accent"] not in accents.ACCENTS:
                    return JSONResponse({"error": "unknown accent colour"}, status_code=400)
                db.set_setting(c, "accent", payload["accent"])
            c.commit()
        finally:
            c.close()
        if "accent" in payload and ctx.on_accent:
            ctx.on_accent(accents.get(payload["accent"]))
        ctx.broadcaster.publish("display")            # other open tabs pick the change up
        return {"ok": True}

    @app.post("/api/ships/{ship_id}/fits", dependencies=[Depends(require_token)])
    async def fits_save(ship_id: int, request: Request):
        payload = await request.json() if (await request.body()) else {}
        return _engplan_call(lambda c, fid: {"id": fits_core.save(c, fid, ship_id, str(payload.get("name") or ""))})

    @app.post("/api/fits/{fit_id}/{action}", dependencies=[Depends(require_token)])
    async def fits_action(fit_id: int, action: str, request: Request):
        payload = await request.json() if (await request.body()) else {}

        def act(c, fid):
            if action == "rename":
                fits_core.rename(c, fid, fit_id, str(payload.get("name") or ""))
            elif action == "delete":
                fits_core.delete(c, fid, fit_id)
            else:
                raise LookupError("no such action")
            return {"ok": True}
        return _engplan_call(act)

    @app.post("/api/builds/{build_id}/{action}", dependencies=[Depends(require_token)])
    async def builds_action(build_id: int, action: str, request: Request):
        payload = await request.json() if (await request.body()) else {}

        def act(c, fid):
            if action == "update":
                builds_core.update(c, fid, build_id, {k: v for k, v in payload.items() if k in ("name", "goal_id", "ship_id")},
                                   retarget=bool(payload.get("retarget")))
            elif action == "delete":
                builds_core.delete(c, fid, build_id)
            elif action == "to-plan":
                view = build_views.build_view(c, fid, builds_core.get(c, fid, build_id))
                have = {r[0] for r in c.execute("SELECT template_key FROM eng_plan WHERE fid = ? AND template_key LIKE ?",
                                                (fid, f"build{build_id}:%"))}
                added = 0
                for e in build_views.plan_entries(view):
                    key = f"build{build_id}:{e['module_type']}:{e['blueprint']}:{e['target_grade']}:{e['done_grade']}:{e['experimental'] or ''}"
                    if key not in have:
                        engplan.add(c, fid, e, template_key=key)
                        added += 1
                return {"added": added}
            else:
                raise LookupError("unknown action")
            return {"ok": True}
        return _engplan_call(act)

    @app.post("/api/activities/{activity}/ship", dependencies=[Depends(require_token)])
    async def activity_ship(activity: str, request: Request):
        payload = await request.json()
        return _engplan_call(lambda c, fid: activity_views.set_activity_ship(c, fid, activity, str(payload.get("ship_id") or "")) or {"ok": True})

    @app.post("/api/mining/spots", dependencies=[Depends(require_token)])
    async def mining_spot_add(request: Request):
        payload = await request.json()
        return _engplan_call(lambda c, fid: {"id": activity_views.add_spot(c, fid, payload)})

    @app.post("/api/mining/spots/{spot_id}/delete", dependencies=[Depends(require_token)])
    def mining_spot_delete(spot_id: int):
        return _engplan_call(lambda c, fid: activity_views.delete_spot(c, fid, spot_id) or {"ok": True})

    # -- notes (Phase 5f): files under data/notes/<fid>/, so no database and no live area ---------

    def _notes_call(fn):
        c, fid = _goal_conn()
        c.close()
        try:
            return fn(fid)
        except ValueError as exc:
            return _bad(exc)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such note")

    @app.post("/api/notes", dependencies=[Depends(require_token)])
    async def notes_create(request: Request):
        payload = await request.json() if (await request.body()) else {}
        def create(fid):
            note_id = notes_core.create(ctx.data_dir, fid, str(payload.get("title") or ""), str(payload.get("body") or ""))
            return {"id": note_id, "redirect": f"/notes?n={note_id}&edit=1"}
        return _notes_call(create)

    @app.post("/api/notes/preview", dependencies=[Depends(require_token)])
    async def notes_preview(request: Request):
        payload = await request.json()
        return {"html": str(notes_core.render(str(payload.get("body") or "")))}

    @app.post("/api/notes/{note_id}/{action}", dependencies=[Depends(require_token)])
    async def notes_action(note_id: str, action: str, request: Request):
        payload = await request.json() if (await request.body()) else {}
        def act(fid):
            if action == "update":
                note = notes_core.update(ctx.data_dir, fid, note_id, {k: payload[k] for k in ("title", "body") if k in payload})
                return {"ok": True, "title": note["title"], "updated": note["updated"], "redirect": f"/notes?n={note_id}"}
            if action == "task":
                notes_core.toggle_task(ctx.data_dir, fid, note_id, int(payload.get("index", -1)), bool(payload.get("done")))
                return {"ok": True}
            if action == "pin":
                notes_core.update(ctx.data_dir, fid, note_id, {"pinned": bool(payload.get("pinned"))})
                return {"ok": True, "redirect": f"/notes?n={note_id}"}
            if action == "delete":
                notes_core.delete(ctx.data_dir, fid, note_id)
                return {"ok": True, "redirect": "/notes"}
            raise LookupError(action)
        return _notes_call(act)

    # -- backup and import (Phase 6): .edplanner files under data/backups/ ----------------------

    def _download(payload: bytes, filename: str) -> Response:
        return Response(payload, media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    @app.post("/api/backup/now", dependencies=[Depends(require_token)])
    def backup_now():
        path = ctx.backup_now()
        return {"name": path.name, "redirect": f"/settings/backup?saved={quote(path.name)}"}

    @app.post("/api/backup/export", dependencies=[Depends(require_token)])
    async def backup_export(request: Request):
        wanted = set((await request.json()).get("commanders") or [])
        c = db.connect(ctx.db_path, any_thread=True)
        try:
            fids = [r["fid"] for r in queries.commanders(c) if r["key"] in wanted]
            if not fids:
                return _bad(ValueError("Pick at least one commander to export."))
            payload = backup_archive.export_bytes(c, ctx.data_dir, fids, app_version=ctx.version)
        finally:
            c.close()
        return _download(payload, f"ED Commander Planner {datetime.now():%Y-%m-%d}{backup_archive.SUFFIX}")

    @app.post("/api/backup/download", dependencies=[Depends(require_token)])
    async def backup_download(request: Request):
        name = str((await request.json()).get("name") or "")
        try:
            if "/" in name:
                raise LookupError(name)
            path = backup_archive.resolve(ctx.data_dir, name)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such backup")
        return _download(path.read_bytes(), path.name)

    @app.post("/api/backup/upload", dependencies=[Depends(require_token)])
    async def backup_upload(request: Request):
        payload = await request.body()
        name = unquote(request.headers.get("X-Filename", "")) or "import.edplanner"
        try:
            backup_archive.Archive(payload, name)        # refuse a bad file before keeping it
        except ValueError as exc:
            return _bad(exc)
        ref = backup_archive.stage_upload(ctx.data_dir, name, payload)
        return {"redirect": f"/settings/backup/review?file={quote(ref)}"}

    def _backup_changed():
        ctx.projection.invalidate()
        for area in ("import", "progress", "materials", "live", "goals", "ships"):
            ctx.broadcaster.publish(area)

    @app.post("/api/backup/apply", dependencies=[Depends(require_token)])
    async def backup_apply(request: Request):
        body = await request.json()
        if ctx.import_status.snapshot()["state"] == "running":
            return JSONResponse({"error": "Wait for the journal import to finish, then apply."}, status_code=409)
        ref = str(body.get("file") or "")
        try:
            path = backup_archive.resolve(ctx.data_dir, ref)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such file")
        modes = {str(k): str(v) for k, v in (body.get("modes") or {}).items()}
        choices = {str(k): str(v) for k, v in (body.get("choices") or {}).items() if v in backup_restore.CHOICES}
        c = db.connect(ctx.db_path, any_thread=True)
        try:
            arc = backup_archive.Archive(path, backup_archive.display_name(ref))
            with ctx.backup_lock:
                log_id = backup_restore.import_file(c, ctx.data_dir, arc, backup_restore.Plan(modes, choices),
                                                    app_version=ctx.version, rebuild=projectors.rebuild)
        except ValueError as exc:
            return _bad(exc)
        finally:
            c.close()
        _backup_changed()
        return {"ok": True, "redirect": f"/settings/backup?done={log_id}"}

    @app.post("/api/backup/rollback/{log_id}", dependencies=[Depends(require_token)])
    def backup_rollback(log_id: int):
        if ctx.import_status.snapshot()["state"] == "running":
            return JSONResponse({"error": "Wait for the journal import to finish, then roll back."}, status_code=409)
        c = db.connect(ctx.db_path, any_thread=True)
        try:
            with ctx.backup_lock:
                backup_restore.rollback(c, ctx.data_dir, log_id, app_version=ctx.version, rebuild=projectors.rebuild)
        except LookupError as exc:
            return JSONResponse({"error": str(exc.args[0] if exc.args else exc)}, status_code=409)
        finally:
            c.close()
        _backup_changed()
        return {"ok": True, "redirect": "/settings/backup"}

    @app.post("/api/plan", dependencies=[Depends(require_token)])
    async def plan_settings(request: Request):
        payload = await request.json()
        c, fid = _goal_conn()
        try:
            with c:
                if "reserve_rebuys" in payload:
                    n = str(payload["reserve_rebuys"]).strip()
                    if not n.isdigit() or int(n) > 20:
                        raise ValueError("reserve must be a whole number of rebuys, 0–20")
                    db.set_setting(c, "reserve_rebuys", n)
                if "selling" in payload:
                    ids = sorted({int(x) for x in payload["selling"]})
                    db.set_setting(c, f"selling_ships:{fid}", json.dumps(ids))
        except ValueError as exc:
            return _bad(exc)
        finally:
            c.close()
        _goals_changed()
        return {"ok": True}

    @app.post("/api/commanders/select", dependencies=[Depends(require_token)])
    async def select(request: Request):
        body = await request.json()
        c = db.connect(ctx.db_path)
        try:
            ok = queries.select_commander(c, str(body.get("key", "")))
        finally:
            c.close()
        if not ok:
            raise HTTPException(status_code=404, detail="unknown commander")
        return {"ok": True}

    @app.get("/events/stream")
    async def stream(request: Request):
        q = ctx.broadcaster.subscribe()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while not ctx.stop.is_set():
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_SECONDS)
                        yield f"data: {msg}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                ctx.broadcaster.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/icon.svg")

    @app.get("/icon.svg", include_in_schema=False)
    def icon(accent: str | None = None, c=Depends(conn)):
        """The planner mark in the chosen accent (the tab icon). `accent` previews another colour."""
        hex_colour = accents.get(accent if accent in accents.ACCENTS else db.get_setting(c, "accent")).hex
        svg = (resources / "static" / "favicon.svg").read_text(encoding="utf-8").replace("#ff7a00", hex_colour)
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})

    return app
