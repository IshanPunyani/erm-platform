import os, json
from fastapi import FastAPI, Request, Form, File, UploadFile, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime
import io

from database import *

app = FastAPI(title="ERM Platform")
init_db()

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
templates = Jinja2Templates(directory="templates")

CATEGORIES  = ["Strategic","Financial","Operational","Regulatory/Compliance","Technology","ESG","Reputational","Legal","Third-Party/Vendor","People & Culture"]
STRATEGIES  = ["Accept","Mitigate","Transfer","Avoid"]
STATUSES    = ["Not Started","In Progress","Completed","Overdue","On Hold","Pending Approval"]
FREQUENCIES = ["Monthly","Quarterly","Semi-Annually","Annually"]
ALERT_ROLES = ["Risk Owner","Mitigation Owner","Risk Team","Risk Officer","MD","CEO"]
ALERT_EVENTS= ["New Risk Added","Risk Score Changed","Mitigation Updated","Assessment Due","Risk Overdue","Rating Escalated","Review Submitted","Review Rejected"]

def base_context(request: Request):
    s = get_settings()
    notifs = get_notifications()
    unread = sum(1 for n in notifs if not n["read"])
    return {"request": request, "settings": s, "notifs": notifs, "unread": unread,
            "now": datetime.now().strftime("%Y-%m-%d")}

# ── DASHBOARD ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Auto-open Due review periods for any risks past their next_assessment_date
    open_due_reviews_all()
    ctx = base_context(request)
    view_mode = request.query_params.get("view", "2d")
    ctx["view_mode"] = view_mode
    ctx["risks"]     = get_risks()
    ctx["businesses"] = get_businesses()
    ctx["movement"]  = get_portfolio_movement()
    ctx["pending_reviews"] = get_pending_reviews()
    return templates.TemplateResponse("dashboard.html", ctx)

# ── RISK REGISTER ─────────────────────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
async def register(request: Request, cat: str="", rating: str="", status: str="", biz: str="", q: str=""):
    ctx = base_context(request)
    risks = get_risks()
    if q:
        ql = q.lower()
        risks = [r for r in risks if ql in (r.get("no","") or "").lower()
                 or ql in (r.get("statement","") or "").lower()
                 or ql in (r.get("category","") or "").lower()
                 or ql in (r.get("risk_owner","") or "").lower()]
    if cat:    risks = [r for r in risks if r.get("category") == cat]
    if rating: risks = [r for r in risks if r.get("rating") == rating]
    if status: risks = [r for r in risks if r.get("status") == status]
    if biz:    risks = [r for r in risks if r.get("applicability") == "group" or biz in (r.get("businesses") or [])]
    ctx.update({"risks": risks, "all_risks": get_risks(),
                "categories": CATEGORIES, "statuses": STATUSES,
                "businesses": get_businesses(),
                "ratings": ["Low","Medium","High","Critical"],
                "filters": {"cat": cat, "rating": rating, "status": status, "biz": biz, "q": q}})
    return templates.TemplateResponse("register.html", ctx)

# ── ADD RISK ──────────────────────────────────────────────────────────────────

@app.get("/risks/add", response_class=HTMLResponse)
async def add_risk_form(request: Request):
    ctx = base_context(request)
    ctx.update({"risk": None, "categories": CATEGORIES, "strategies": STRATEGIES,
                "statuses": STATUSES, "frequencies": FREQUENCIES,
                "businesses": get_businesses(),
                "impact_scale": get_impact_scale(),
                "likelihood_scale": get_likelihood_scale(),
                "velocity_scale": get_velocity_scale()})
    return templates.TemplateResponse("risk_form.html", ctx)

@app.post("/risks/add")
async def add_risk_post(request: Request):
    form = await request.form()
    data = {k: v for k, v in form.items()}
    # Handle multi-select businesses
    data["businesses"] = form.getlist("businesses")
    create_risk(data)
    return RedirectResponse("/register?msg=added", status_code=303)

# ── EDIT RISK ─────────────────────────────────────────────────────────────────

@app.get("/risks/{rid}/edit", response_class=HTMLResponse)
async def edit_risk_form(request: Request, rid: int):
    risk = get_risk(rid)
    if not risk:
        return RedirectResponse("/register")
    ctx = base_context(request)
    ctx.update({"risk": risk, "categories": CATEGORIES, "strategies": STRATEGIES,
                "statuses": STATUSES, "frequencies": FREQUENCIES,
                "businesses": get_businesses(),
                "impact_scale": get_impact_scale(),
                "likelihood_scale": get_likelihood_scale(),
                "velocity_scale": get_velocity_scale(),
                "approval_matrix": get_approval_matrix()})
    return templates.TemplateResponse("risk_form.html", ctx)

@app.post("/risks/{rid}/edit")
async def edit_risk_post(request: Request, rid: int):
    form = await request.form()
    data = {k: v for k, v in form.items()}
    data["businesses"] = form.getlist("businesses")
    update_risk(rid, data)
    return RedirectResponse(f"/risks/{rid}/view?msg=updated", status_code=303)

# ── VIEW RISK ─────────────────────────────────────────────────────────────────

@app.get("/risks/{rid}/view", response_class=HTMLResponse)
async def view_risk(request: Request, rid: int):
    # Make sure period is opened if due before rendering
    open_review_if_due(rid)
    risk = get_risk(rid)
    if not risk: return RedirectResponse("/register")
    ctx = base_context(request)
    ctx.update({"risk": risk, "approval_matrix": get_approval_matrix(),
                "impact_scale": get_impact_scale(),
                "likelihood_scale": get_likelihood_scale(),
                "velocity_scale": get_velocity_scale(),
                "msg": request.query_params.get("msg","")})
    return templates.TemplateResponse("risk_detail.html", ctx)

# ── DELETE RISK ───────────────────────────────────────────────────────────────

@app.post("/risks/{rid}/delete")
async def del_risk(rid: int):
    delete_risk(rid)
    return RedirectResponse("/register?msg=deleted", status_code=303)

# ── SUBMIT PROPOSED PLAN ──────────────────────────────────────────────────────

@app.post("/risks/{rid}/submit-plan")
async def submit_plan(request: Request, rid: int):
    form = await request.form()
    conn = get_db()
    conn.execute("""UPDATE risks SET proposed_plan=?,proposed_plan_submitted_by=?,
        proposed_plan_submitted_at=datetime('now'),updated_at=datetime('now') WHERE id=?""",
        (form.get("proposed_plan"), form.get("submitted_by"), rid))
    conn.commit(); conn.close()
    return RedirectResponse(f"/risks/{rid}/view?msg=plan_submitted", status_code=303)

# ── APPROVE / REJECT RISK ─────────────────────────────────────────────────────

@app.post("/risks/{rid}/approve")
async def approve_risk_post(request: Request, rid: int):
    form = await request.form()
    approve_risk(rid, {"status": form.get("decision"), "approver": form.get("approver"),
                       "approved_by": form.get("approved_by"), "comments": form.get("comments","")})
    return RedirectResponse(f"/risks/{rid}/view?msg=approved", status_code=303)

# ── RATING MATRIX ─────────────────────────────────────────────────────────────

@app.get("/matrix", response_class=HTMLResponse)
async def matrix(request: Request):
    ctx = base_context(request)
    ctx.update({"impact_scale": get_impact_scale(), "likelihood_scale": get_likelihood_scale(),
                "velocity_scale": get_velocity_scale(),
                "msg": request.query_params.get("msg","")})
    return templates.TemplateResponse("matrix.html", ctx)

@app.post("/matrix/impact")
async def save_impact(request: Request):
    form = await request.form()
    rows = []
    for i in range(1, 6):
        rows.append({"score": i, "label": form.get(f"label_{i}",""),
                     "description": form.get(f"desc_{i}",""),
                     "fin_threshold": form.get(f"fin_{i}","")})
    save_impact_scale(rows)
    return RedirectResponse("/matrix?msg=impact_saved", status_code=303)

@app.post("/matrix/likelihood")
async def save_likelihood(request: Request):
    form = await request.form()
    rows = []
    for i in range(1, 6):
        rows.append({"score": i, "label": form.get(f"label_{i}",""),
                     "description": form.get(f"desc_{i}",""),
                     "probability": form.get(f"prob_{i}","")})
    save_likelihood_scale(rows)
    return RedirectResponse("/matrix?msg=likelihood_saved", status_code=303)

@app.post("/matrix/velocity")
async def save_velocity(request: Request):
    form = await request.form()
    rows = []
    for i in range(1, 4):  # velocity is 1–3
        rows.append({"score": i, "label": form.get(f"vlabel_{i}",""),
                     "description": form.get(f"vdesc_{i}","")})
    save_velocity_scale(rows)
    return RedirectResponse("/matrix?msg=velocity_saved", status_code=303)

# ── PERIODIC REVIEWS ──────────────────────────────────────────────────────────

@app.get("/reviews", response_class=HTMLResponse)
async def reviews_page(request: Request):
    open_due_reviews_all()
    ctx = base_context(request)
    ctx["pending"] = get_pending_reviews()
    ctx["msg"] = request.query_params.get("msg","")
    return templates.TemplateResponse("reviews.html", ctx)

@app.post("/risks/{rid}/review")
async def submit_review_post(request: Request, rid: int):
    form = await request.form()
    # Handle attachments (multiple files)
    atts = []
    upload_dir = os.path.join("uploads", "reviews", str(rid))
    os.makedirs(upload_dir, exist_ok=True)
    files = []
    try:
        files = form.getlist("attachments")
    except Exception:
        files = []
    for f in files:
        if hasattr(f, "filename") and f.filename:
            safe = os.path.basename(f.filename).replace(" ","_")
            stamped = datetime.now().strftime("%Y%m%d_%H%M%S_") + safe
            path = os.path.join(upload_dir, stamped)
            with open(path, "wb") as fh:
                fh.write(await f.read())
            atts.append({"filename": safe, "path": f"/uploads/reviews/{rid}/{stamped}"})
    data = {
        "submitted_by":      form.get("submitted_by",""),
        "impact_financial":  form.get("impact_financial"),
        "impact_operational":form.get("impact_operational"),
        "impact_regulatory": form.get("impact_regulatory"),
        "impact_brand":      form.get("impact_brand"),
        "likelihood":        form.get("likelihood"),
        "velocity":          form.get("velocity"),
        "comments":          form.get("comments",""),
        "attachments":       atts,
    }
    submit_review(rid, data)
    return RedirectResponse(f"/risks/{rid}/view?msg=review_submitted", status_code=303)

@app.post("/reviews/{review_id}/decide")
async def decide_review_post(request: Request, review_id: int):
    form = await request.form()
    decide_review(review_id, form.get("decision",""), form.get("approver_role",""),
                  form.get("approved_by",""), form.get("comments",""))
    rid = form.get("risk_id","")
    if rid:
        return RedirectResponse(f"/risks/{rid}/view?msg=review_decided", status_code=303)
    return RedirectResponse("/reviews?msg=decided", status_code=303)

# ── MASTERS ───────────────────────────────────────────────────────────────────

@app.get("/masters", response_class=HTMLResponse)
async def masters(request: Request):
    ctx = base_context(request)
    ctx.update({"businesses": get_businesses(),
                "contacts": get_contacts(),
                "approval_matrix": get_approval_matrix(),
                "alert_roles": ALERT_ROLES,
                "alert_events": ALERT_EVENTS,
                "msg": request.query_params.get("msg","")})
    return templates.TemplateResponse("masters.html", ctx)

@app.post("/masters/business/add")
async def add_business(request: Request):
    form = await request.form()
    create_business(form.get("name","").strip())
    return RedirectResponse("/masters?msg=biz_added", status_code=303)

@app.post("/masters/business/{bid}/edit")
async def edit_business(bid: int, request: Request):
    form = await request.form()
    update_business(bid, form.get("name",""), int(form.get("active",1)))
    return RedirectResponse("/masters?msg=biz_updated", status_code=303)

@app.post("/masters/business/{bid}/delete")
async def del_business(bid: int):
    delete_business(bid)
    return RedirectResponse("/masters?msg=biz_deleted", status_code=303)

@app.post("/masters/contact/save")
async def save_contact(request: Request):
    form = await request.form()
    data = {"id": form.get("id") or None, "name": form.get("name",""),
            "email": form.get("email",""), "role": form.get("role",""),
            "active": 1 if form.get("active") else 0,
            "alerts": form.getlist("alerts")}
    if data["id"]: data["id"] = int(data["id"])
    upsert_contact(data)
    return RedirectResponse("/masters?msg=contact_saved", status_code=303)

@app.post("/masters/contact/{cid}/delete")
async def del_contact(cid: int):
    delete_contact(cid)
    return RedirectResponse("/masters?msg=contact_deleted", status_code=303)

@app.post("/masters/approval-matrix")
async def save_approval(request: Request):
    form = await request.form()
    ratings_list = form.getlist("rating")
    roles_list   = form.getlist("approver_role")
    names_list   = form.getlist("approver_name")
    orders_list  = form.getlist("order_no")
    rows = []
    for i in range(len(ratings_list)):
        if ratings_list[i]:
            try:
                order_no = int(orders_list[i]) if i < len(orders_list) else 1
            except (ValueError, TypeError):
                order_no = 1
            rows.append({"rating": ratings_list[i], "approver_role": roles_list[i] if i < len(roles_list) else "",
                         "approver_name": names_list[i] if i < len(names_list) else "",
                         "order_no": order_no})
    save_approval_matrix(rows)
    return RedirectResponse("/masters?msg=matrix_saved", status_code=303)

# ── DOCUMENTS ─────────────────────────────────────────────────────────────────

@app.get("/documents", response_class=HTMLResponse)
async def documents(request: Request):
    ctx = base_context(request)
    ctx.update({"documents": get_documents(), "msg": request.query_params.get("msg","")})
    return templates.TemplateResponse("documents.html", ctx)

@app.post("/documents/save")
async def save_doc(request: Request):
    form = await request.form()
    filename = ""
    file = form.get("file")
    if file and hasattr(file, "filename") and file.filename:
        safe = os.path.basename(file.filename).replace(" ", "_")
        path = os.path.join("uploads", safe)
        with open(path, "wb") as f:
            f.write(await file.read())
        filename = safe
    data = {"id": form.get("id") or None, "doc_type": form.get("doc_type",""),
            "title": form.get("title",""), "content": form.get("content",""),
            "filename": filename or form.get("existing_filename",""),
            "uploaded_by": form.get("uploaded_by","")}
    if data["id"]: data["id"] = int(data["id"])
    upsert_document(data)
    return RedirectResponse("/documents?msg=saved", status_code=303)

@app.post("/documents/{did}/delete")
async def del_doc(did: int):
    delete_document(did)
    return RedirectResponse("/documents?msg=deleted", status_code=303)

# ── EXCEL IMPORT ──────────────────────────────────────────────────────────────

@app.post("/import-excel")
async def import_excel(request: Request):
    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "filename") or not file.filename:
        return RedirectResponse("/register?msg=No+file+provided", status_code=303)
    safe_filename = os.path.basename(file.filename)
    path = f"/tmp/erm_import_{safe_filename}"
    with open(path, "wb") as f:
        f.write(await file.read())
    added_by = form.get("added_by", "Imported")
    imported, errors = import_risks_from_excel(path, added_by)
    os.remove(path)
    msg = f"Imported {imported} risks."
    if errors: msg += " Errors: " + "; ".join(errors[:3])
    return RedirectResponse(f"/register?msg={msg}", status_code=303)

# ── EXPORTS ───────────────────────────────────────────────────────────────────

@app.get("/export/csv")
async def export_csv():
    csv_data = export_risks_csv()
    fname = f"erm_register_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(io.StringIO(csv_data), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"})

@app.get("/export/template")
async def export_template():
    data = generate_excel_template()
    return StreamingResponse(io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=erm_template.xlsx"})

# ── SETTINGS ──────────────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    ctx = base_context(request)
    ctx["msg"] = request.query_params.get("msg","")
    return templates.TemplateResponse("settings.html", ctx)

@app.post("/settings")
async def save_settings_post(request: Request):
    form = await request.form()
    save_settings({k: v for k, v in form.items()})
    return RedirectResponse("/settings?msg=saved", status_code=303)

@app.post("/settings/logo")
async def upload_logo(file: UploadFile = File(...)):
    import base64
    data = await file.read()
    b64 = "data:" + (file.content_type or "image/png") + ";base64," + base64.b64encode(data).decode()
    save_settings({"company_logo": b64})
    return RedirectResponse("/settings?msg=logo_saved", status_code=303)

# ── NOTIFICATIONS API ─────────────────────────────────────────────────────────

@app.get("/api/notifications")
async def api_notifs():
    return get_notifications()

@app.post("/api/notifications/clear")
async def api_clear_notifs():
    clear_notifications()
    return {"ok": True}

@app.get("/api/risks/{rid}")
async def api_get_risk(rid: int):
    return get_risk(rid)

# ── STATUS APPROVAL WORKFLOW ───────────────────────────────────────────────────

@app.post("/risks/{risk_id}/submit-status")
async def submit_status(
    request: Request,
    risk_id: int,
    pending_status: str = Form(...),
    submitted_by: str = Form(...)
):
    conn = get_db()
    try:
        conn.execute("""
            UPDATE risks
            SET pending_status=?,
                pending_status_submitted_by=?,
                pending_status_submitted_at=?,
                pending_status_approval='Pending'
            WHERE id=?
        """, (
            pending_status,
            submitted_by,
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            risk_id
        ))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/risks/{risk_id}/view",
        status_code=303
    )

@app.post("/risks/{risk_id}/approve-status")
async def approve_status(
    request: Request,
    risk_id: int,
    decision: str = Form(...)
):
    conn = get_db()
    try:
        risk = conn.execute("""
            SELECT pending_status
            FROM risks
            WHERE id=?
        """, (risk_id,)).fetchone()

        if decision == "Approved":
            conn.execute("""
                UPDATE risks
                SET status=?,
                    pending_status_approval='Approved'
                WHERE id=?
            """, (
                risk["pending_status"],
                risk_id
            ))
        else:
            conn.execute("""
                UPDATE risks
                SET pending_status_approval='Rejected'
                WHERE id=?
            """, (risk_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/risks/{risk_id}/view",
        status_code=303
    )

# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)