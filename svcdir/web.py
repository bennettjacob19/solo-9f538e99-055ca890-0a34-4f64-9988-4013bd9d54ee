"""Flask application: HTML directory editor plus the ``/resolve`` API."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .errors import DirectoryError, InputError
from .store import DirectoryStore


def _store_from_env() -> DirectoryStore:
    db_path = os.environ.get("SVCDIR_DB", str(Path("data") / "directory.sqlite3"))
    lease = float(os.environ.get("SVCDIR_RESTORE_LEASE_SECONDS", "30"))
    return DirectoryStore(db_path, restore_lease_seconds=lease)


def create_app(store: DirectoryStore | None = None, *, config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SVCDIR_TESTING=False,
        JSON_AS_ASCII=False,
    )
    if config:
        app.config.update(config)
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    store = store or _store_from_env()
    app.extensions["svcdir"] = store
    store.initialize()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'none'",
        )
        return response

    # -- error handling ---------------------------------------------------

    @app.errorhandler(DirectoryError)
    def handle_directory_error(exc: DirectoryError):
        if _wants_json():
            return jsonify({"ok": False, "error": exc.to_dict()}), exc.status_code
        return (
            render_template(
                "error.html",
                title="操作未完成",
                status=exc.status_code,
                error=exc.to_dict(),
            ),
            exc.status_code,
        )

    @app.errorhandler(404)
    def handle_404(exc):
        if _wants_json():
            return (
                jsonify({"ok": False, "error": {"code": "not_found", "message": "接口不存在"}}),
                404,
            )
        return render_template("error.html", title="未找到", status=404,
                               error={"code": "not_found", "message": "页面或接口不存在"}), 404

    @app.errorhandler(405)
    def handle_405(exc):
        if _wants_json():
            return (
                jsonify({"ok": False, "error": {"code": "method_not_allowed", "message": "方法不允许"}}),
                405,
            )
        return render_template("error.html", title="方法不允许", status=405,
                               error={"code": "method_not_allowed", "message": "请求方法不被允许"}), 405

    @app.errorhandler(500)
    def handle_500(exc):
        original = getattr(exc, "original_exception", None) or exc
        app.logger.error("unhandled error: %s", original, exc_info=original)
        if _wants_json():
            return (
                jsonify({"ok": False, "error": {"code": "internal_error", "message": "服务器内部错误"}}),
                500,
            )
        return render_template("error.html", title="服务器错误", status=500,
                               error={"code": "internal_error", "message": "服务器内部错误"}), 500

    def _wants_json() -> bool:
        if request.path.startswith("/api/") or request.path == "/resolve":
            return True
        return request.accept_mimetypes["application/json"] > request.accept_mimetypes["text/html"]

    # -- API --------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        store.ping()
        return jsonify({"ok": True})

    @app.get("/resolve")
    def resolve():
        name = request.args.get("name")
        request_id = request.args.get("request_id")
        if request_id is not None:
            request_id = request_id.strip()
        if request_id:
            # A request id only applies to a single-name lookup; the receipt
            # records exactly one service/version/answer triple.
            if not name or not name.strip():
                raise InputError("带请求号的查询必须指定单个服务名（name）")
            result = store.resolve_with_receipt(name, request_id)
            body = {
                "ok": True,
                "version": result["version"],
                "restore_in_progress": result["restore_in_progress"],
                "name": result["name"],
                "found": result["found"],
                "request_id": result["request_id"],
                "replay": result["replay"],
                "receipt": result["receipt"],
            }
            if result["found"]:
                body["address"] = result["address"]
            return jsonify(body), 200

        with store.read_view() as view:
            result = view.resolve(name)
        body = {
            "ok": True,
            "version": result["version"],
            "restore_in_progress": result["restore_in_progress"],
        }
        if "address" in result:
            body["name"] = result["name"]
            body["address"] = result["address"]
        else:
            body["count"] = result["count"]
            body["services"] = result["services"]
        return jsonify(body)

    @app.get("/api/state")
    def api_state():
        return jsonify({"ok": True, **store.snapshot()})

    @app.get("/api/backups")
    def api_backups():
        return jsonify({"ok": True, "backups": store.list_backups()})

    @app.post("/api/services")
    def api_upsert():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise InputError("请求体必须是 JSON 对象")
        result = store.upsert(
            payload.get("service"), payload.get("address"), payload.get("note", "")
        )
        return jsonify({"ok": True, **result}), 200

    @app.delete("/api/services/<path:service>")
    def api_delete(service: str):
        return jsonify({"ok": True, **store.delete(service)})

    @app.post("/api/backups")
    def api_create_backup():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise InputError("请求体必须是 JSON 对象")
        result = store.create_backup(payload.get("name"))
        return jsonify({"ok": True, **result}), 201

    @app.get("/api/backups/<path:name>/validate")
    def api_validate_backup(name: str):
        return jsonify({"ok": True, **store.validate_backup(name)})

    @app.post("/api/backups/<path:name>/restore")
    def api_restore_backup(name: str):
        result = store.restore_backup(name)
        return jsonify({"ok": True, **result})

    @app.get("/api/receipts/<request_id>")
    def api_get_receipt(request_id: str):
        return jsonify({"ok": True, "receipt": store.get_receipt(request_id)})

    @app.get("/api/receipts")
    def api_list_receipts():
        try:
            limit = int(request.args.get("limit", "20"))
        except ValueError:
            raise InputError("limit 必须是整数")
        return jsonify({"ok": True, "receipts": store.list_receipts(limit)})

    # -- HTML -------------------------------------------------------------

    def _redirect_back(**kwargs):
        return redirect(url_for("index", **kwargs), code=303)

    @app.get("/")
    def index():
        snapshot = store.snapshot()
        return render_template("index.html", state=snapshot, message=request.args.get("message"),
                               error=request.args.get("error"))

    @app.post("/services")
    def form_upsert():
        try:
            result = store.upsert(
                request.form.get("service"), request.form.get("address"), request.form.get("note", "")
            )
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已保存 {result['service']} -> {result['address']}（目录版本 {result['version']}）"
        )

    @app.post("/services/<path:service>/delete")
    def form_delete(service: str):
        try:
            result = store.delete(service)
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(message=f"已删除 {result['service']}（目录版本 {result['version']}）")

    @app.post("/backups")
    def form_create_backup():
        try:
            result = store.create_backup(request.form.get("name"))
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已创建备份 {result['name']}（{result['entry_count']} 条，版本 {result['version']}）"
        )

    @app.post("/backups/<path:name>/restore")
    def form_restore_backup(name: str):
        try:
            result = store.restore_backup(name)
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已从备份 {result['backup']} 恢复：{result['entry_count']} 条，"
                    f"新目录版本 {result['new_version']}"
        )

    @app.post("/resolve-trial")
    def form_resolve_trial():
        """Browser-side trial of /resolve, optionally pinned by a request id."""
        name = (request.form.get("service") or "").strip()
        request_id = (request.form.get("request_id") or "").strip()
        try:
            if request_id:
                if not name:
                    raise InputError("带请求号的查询必须指定单个服务名")
                result = store.resolve_with_receipt(name, request_id)
                if result["found"]:
                    verb = "回执重放" if result["replay"] else "已登记回执并应答"
                    message = (
                        f"{verb}：{result['name']} -> {result['address']}"
                        f"（目录版本 {result['version']}，请求号 {result['request_id']}）"
                    )
                else:
                    verb = "回执重放" if result["replay"] else "已登记未找到回执"
                    message = (
                        f"{verb}：服务 {result['name']} 未找到"
                        f"（目录版本 {result['version']}，请求号 {result['request_id']}）"
                    )
            else:
                if not name:
                    raise InputError("请输入服务名")
                result = store.resolve(name)
                message = (
                    f"当前查询（无请求号，不登记回执）：{result['name']} -> "
                    f"{result['address']}（目录版本 {result['version']}）"
                )
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(message=message)

    @app.get("/receipts/<request_id>")
    def form_receipt_detail(request_id: str):
        receipt = store.get_receipt(request_id)
        return render_template("receipt.html", receipt=receipt)

    return app
