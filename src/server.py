"""
========================================
server.py — MCP 服务入口 + Dashboard HTTP 路由 + 启动装配
========================================

启动整个 Ombre Brain 进程：加载配置、创建 BucketManager / Dehydrator /
DecayEngine / EmbeddingEngine / ImportEngine，把它们注入 tools._runtime，
然后以 @mcp.tool() 注册薄封装（真正的实现在 src/tools/<工具>/ 下面）。

关键行为：
- 启动后暴露 12 个 MCP 工具：breath/hold/grow/trace/anchor/release/
  pulse/plan/letter_write/letter_read/dream/I；每个入口 ≤ 10 行，只负责转发
- 同时开 Dashboard HTTP 服务：@mcp.custom_route() 下的路由都留在本文件
- 提供会话 / 鉴权 / Webhook / SSE 推送 / 压力表 / heartbeat 等走 HTTP 的能力
- 企业级细节：CSRF token / rate limit / nonce 去重 / TLS 提示

不做什么（边界）：
- 不在这里写 hold/breath/dream 等业务逻辑（全在 tools/* 下）
- 不写 LLM prompt（dehydrator 负责）
- 不直接读写桶文件（bucket_manager 负责）

对外暴露：mcp/mcp_extra 两个实例 + 12 个 @mcp*.tool() 函数 + 一批 @mcp.custom_route HTTP 接口
========================================
"""

import os
import sys
import random
import logging
import asyncio
import hashlib
import hmac
import secrets
import time
import json as _json_lib
from typing import Optional, Awaitable
from starlette.requests import Request
from starlette.responses import Response
import httpx
import yaml


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from migrate_engine import MigrateEngine
from utils import load_config, setup_logging, strip_wikilinks, count_tokens_approx, get_version, extract_wikilinks

# --- iter 2.1：MCP 工具实现已按代码路径拆分到 tools/ 子包 ---
# 本文件只保留 MCP 注册 + 路由（HTTP custom_route）+ 共享辅助。
# 真正的工具逻辑在 tools/breath, tools/hold, tools/grow, tools/trace,
# tools/anchor, tools/plan, tools/dream 里，便于单独阅读和修改。
from tools import _runtime as _tools_runtime
from tools import breath as _t_breath
from tools import hold as _t_hold
from tools import grow as _t_grow
from tools import trace as _t_trace
from tools import anchor as _t_anchor
from tools import plan as _t_plan
from tools import dream as _t_dream
from tools import i as _t_i
from tools._common import (
    check_content_size as _check_content_size,
    check_pinned_quota as _check_pinned_quota,
)

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Project version (read from <repo_root>/VERSION) / 项目版本号 ---
# get_version() 汇总读文件 + fallback 逻辑。
# 赋给双下划线变量 `__version__` 是 Python 社区约定俗成的模块版本字段名。
__version__ = get_version()
logger.info(f"Ombre Brain v{__version__}")

# --- iter 1.7 §A: legacy path migration check / 老路径迁移检测 ---
# 场景：1.6 早期使用者习惯在项目根跑 `python server.py`；1.7 重组后需要
# `python src/server.py`。这里只做「检测 + 提醒」，不做任何破坏性动作。
# load_config() 里 buckets_dir 默认仍是 <repo_root>/buckets，所以老数据不会丢。
#
# Python 小知识：
#   * 变量名以 `_` 开头是「模块内部」约定，不是语法强制
#   * for/else 这里没用，用了 break 提前退出
#   * `os.path.isdir(p) and any(...)` 是短路：前者 False 就不会跳 listdir
try:
    _bd = config.get("buckets_dir", "")
    if _bd and os.path.isdir(_bd):
        _has_data = False
        # 遍历各个桃子目录，任何一个里有 .md 文件就认定早期部署位置有数据
        for sub in ("permanent", "dynamic", "feel", "plans", "letters"):
            p = os.path.join(_bd, sub)
            if os.path.isdir(p) and any(
                f.endswith(".md") for f in os.listdir(p) if not f.startswith(".")
            ):
                _has_data = True
                break
        if _has_data:
            logger.info(f"[migration] existing buckets detected at {_bd} — zero data loss expected.")
        else:
            logger.info(f"[migration] {_bd} is empty — fresh install assumed.")
except Exception as _e:  # pragma: no cover - defensive / 防御性兑底
    # 启动期任何检测出错都不能阻止服务拉起，记个 warning 就过
    logger.warning(f"[migration] check skipped: {_e}")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 8000
try:
    OMBRE_PORT = int(os.environ.get("OMBRE_PORT", "8000") or "8000")
except ValueError:
    logger.warning("OMBRE_PORT 不是合法整数，回退到 8000")
    OMBRE_PORT = 8000

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。
# 详见 ENV_VARS.md。
OMBRE_HOOK_URL = os.environ.get("OMBRE_HOOK_URL", "").strip()
OMBRE_HOOK_SKIP = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §①：禁裸魔法数字。这里集中所有会调的阁值。
# 与安全、鉴权、性能相关的参数不要在运行时乲变；如需调整请同步跑 pytest。
# ============================================================

# --- Webhook / HTTP 客户端超时 ---
_WEBHOOK_TIMEOUT_SECONDS = 5.0
_HEALTH_PROBE_TIMEOUT_SECONDS = 5

# --- Dashboard 鉴权常量 / 会话 / 密码逻辑已移至 web/_shared.py ---

# --- /api/logs 返回行数限制 ---
_LOGS_DEFAULT_LIMIT = 200
_LOGS_MAX_LIMIT = 2000

# --- /api/errors/recent 返回条数限制 ---
_ERRORS_DEFAULT_LIMIT = 50
_ERRORS_MAX_LIMIT = 500


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    if OMBRE_HOOK_SKIP or not OMBRE_HOOK_URL:
        return
    if not OMBRE_HOOK_URL.startswith(("http://", "https://")):
        logger.warning(f"OMBRE_HOOK_URL rejected: only http/https allowed (got {OMBRE_HOOK_URL[:40]!r})")
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            await client.post(OMBRE_HOOK_URL, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {OMBRE_HOOK_URL}): {e}")

# --- Initialize core components / 初始化核心组件 ---
# 统一错误码体系（必须在任何业务初始化之前 configure，确保 errors.jsonl 路径生效）
try:
    from errors import (
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
        recent_errors,
        clear_errors_log,
        get_recent_logs,
    )
except ImportError:
    from .errors import (  # type: ignore
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
        recent_errors,
        clear_errors_log,
        get_recent_logs,
    )
configure_errors_path(config.get("buckets_dir", "buckets"))

try:
    embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
except OBStartupError as _ob_err:
    # OB-F001 已在 OBStartupError 内格式化好；写 fatal log 后退出
    logger.error(str(_ob_err))
    write_fatal_log(_ob_err.error_code, _ob_err.detail, buckets_dir=config.get("buckets_dir"))
    raise
except RuntimeError as _emb_err:
    # 兼容尚未迁移到 OBStartupError 的旧 raise（应该不再触发）
    logger.error(f"[STARTUP FAILED] {_emb_err}")
    raise SystemExit(f"Ombre Brain 启动中止：{_emb_err}") from _emb_err
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎
migrate_engine = MigrateEngine(config, bucket_mgr, embedding_engine)              # Migrate engine / 记忆包迁移引擎

# --- GitHub Sync / GitHub 同步 ---
from github_sync import GitHubSync  # type: ignore
_gh_cfg = config.get("github_sync", {}) or {}
_gh_token = (os.environ.get("OMBRE_GITHUB_TOKEN") or _gh_cfg.get("token") or "").strip()
github_sync_instance: GitHubSync | None = (
    GitHubSync(
        token=_gh_token,
        repo=_gh_cfg.get("repo", ""),
        branch=_gh_cfg.get("branch", "main"),
        path_prefix=_gh_cfg.get("path_prefix", "ombre"),
    )
    if _gh_token and _gh_cfg.get("repo")
    else None
)
_github_auto_task: "asyncio.Task | None" = None  # 后台定时同步任务


async def _github_sync_loop(interval_minutes: int) -> None:
    """后台定时 GitHub 同步循环。只在 is_validated=True 后执行实际上传。"""
    import asyncio
    logger.info(f"[github_sync] auto-sync loop started, interval={interval_minutes}min")
    # 首次先做一次验证，确认连接可用
    if github_sync_instance and not github_sync_instance.is_validated:
        try:
            result = await github_sync_instance.validate()
            if not result.get("ok"):
                logger.warning(f"[github_sync] auto-sync: validate failed: {result.get('error')} — loop will retry next cycle")
        except Exception as e:
            logger.warning(f"[github_sync] auto-sync: validate exception: {e}")
    while True:
        await asyncio.sleep(interval_minutes * 60)
        inst = github_sync_instance  # 读当前全局引用（config 更新可能替换实例）
        if inst is None:
            logger.info("[github_sync] auto-sync: instance gone, stopping loop")
            return
        if not inst.is_validated:
            # 还没验证通过，先 validate
            try:
                res = await inst.validate()
                if not res.get("ok"):
                    logger.warning(f"[github_sync] auto-sync skipped (not validated): {res.get('error')}")
                    continue
            except Exception as e:
                logger.warning(f"[github_sync] auto-sync validate failed: {e}")
                continue
        buckets_dir = config.get("buckets_dir", "")
        if not buckets_dir:
            continue
        try:
            result = await inst.sync(buckets_dir)
            if result.get("ok"):
                logger.info(f"[github_sync] auto-sync ok: {result.get('uploaded', 0)} files")
            else:
                logger.warning(f"[github_sync] auto-sync failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"[github_sync] auto-sync exception: {e}")


def _restart_github_auto_task(interval_minutes: int) -> None:
    """取消旧任务并按新间隔启动后台同步循环（interval_minutes=0 表示仅取消）。"""
    import asyncio
    global _github_auto_task
    if _github_auto_task and not _github_auto_task.done():
        _github_auto_task.cancel()
        _github_auto_task = None
    if interval_minutes > 0 and github_sync_instance is not None:
        try:
            loop = asyncio.get_event_loop()
            _github_auto_task = loop.create_task(_github_sync_loop(interval_minutes))
        except RuntimeError:
            pass  # 没有运行中的 event loop（测试环境），跳过


# 启动时若配置了自动同步间隔，推迟到事件循环就绪后启动（用 lifespan 钩子）
_gh_auto_interval: int = int(_gh_cfg.get("auto_interval_minutes") or 0)


# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
#
# iter 2.1：拆成两个 FastMCP 实例 —— 因 claude.ai MCP 连接器存在 5 工具上限。
#   主 mcp（/mcp）：高频  breath / hold / grow / dream / trace
#   副 mcp_extra（/mcp-extra）：低频 anchor / release / pulse / plan / letter_write / letter_read / I
# 两个实例共享同一进程、同一 runtime、同一 bucket_mgr；HTTP custom_route（dashboard、API）
# 全部仍挂在 mcp 主实例上，副实例只承载 7 个 @mcp_extra.tool() 注册。
# 启动段把两个 streamable_http_app() 的 routes 与 lifespan 合并到一个 starlette app，
# 由同一 uvicorn 进程对外暴露。
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
)
mcp_extra = FastMCP(
    "Ombre Brain Extra",
    host="0.0.0.0",
    port=OMBRE_PORT,
)
# 让 streamable_http_app() 内置路由直接落在 /mcp-extra（默认是 /mcp）
mcp_extra.settings.streamable_http_path = "/mcp-extra"


# =============================================================
# Dashboard Auth —— 已拆分：会话/密码/鉴权 helper 在 web/_shared.py，
# /auth/* 路由在 web/auth.py。这里注入 config，并把 helper 名字 import 回本模块，
# 让本文件其余尚未迁移的 @mcp.custom_route 路由（大量调用 _require_auth）继续可用；
# 待这些路由也迁出 web/ 后，本段 import 可删除。
# =============================================================
import web as _web
import web._shared as _wsh
_wsh.init(config)
# 注入业务引擎/版本/仓库根目录到 web 层（类比 tools/_runtime）。
# 注意：embedding_engine 会被热重载替换 —— 待 embedding/config 路由迁到 web/ 时，
# 替换处须同时写 _wsh.embedding_engine（目前这些路由仍在本文件、仍走 global）。
_wsh.init_runtime(
    version=__version__,
    repo_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    import_engine=import_engine,
    migrate_engine=migrate_engine,
    github_sync_instance=github_sync_instance,
)
from web._shared import (  # noqa: F401  (re-export for not-yet-migrated routes below)
    _sessions,
    _is_authenticated, _is_setup_needed, _verify_any_password,
    _create_session, _set_session_cookie, _is_https_request,
    _load_auth_data, _load_password_hash, _save_password_hash,
    _save_security_qa, _verify_security_answer, _verify_password_hash,
    _load_sessions, _save_sessions, _require_auth,
)

# 启动时把磁盘上的会话装回内存（容器重启不踢登录）
_load_sessions()

# 注册已迁移到 web/ 的路由（目前：/auth/*）
_web.register_all(mcp)


# =============================================================
# 根仪表板 / 静态资源 / favicon / /health —— 已拆分到 web/dashboard.py
# =============================================================


# 心跳时间戳 + _mark_op 已移到 web/_shared.py；这里 import 回来供 tools._runtime 注入。
from web._shared import _mark_op  # noqa: F401  (injected into tools._runtime below)


# =============================================================
# 仪表板硬删除通知队列（Dashboard Hard Purge Notification）
# 她/他从仪表板彻底删除记忆后，下次 Claude 调用任何工具时一次性通知。
# 通知文件存于 buckets_dir/_pending_deletions.json，消费后立即删除。
# Claude 无法触发此通知（它不是 MCP 工具，只能由仪表板 HTTP 端点写入）。
# =============================================================

def _deletion_notice_path() -> str:
    return os.path.join(config.get("buckets_dir", "buckets"), "_pending_deletions.json")


def _write_deletion_notice(names: list) -> None:
    """追加待发送删除通知。多次删除批次会合并入同一文件直至 Claude 读取。"""
    path = _deletion_notice_path()
    try:
        existing: list = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = _json_lib.load(f)
        existing.extend(names)
        with open(path, "w", encoding="utf-8") as f:
            _json_lib.dump(existing, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to write deletion notice: {e}")


def _pop_deletion_notice() -> str:
    """读取并消费通知文件。返回格式化通知字符串（含尾部换行），无通知返回空串。"""
    path = _deletion_notice_path()
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            names = _json_lib.load(f)
        os.remove(path)
        if not names:
            return ""
        human = config.get("human", "人类")
        ts = time.strftime("%Y-%m-%d %H:%M")
        item_list = "\n".join(f"  · {n}" for n in names)
        return (
            f"「{ts}，{human} 通过前端界面永久删除了以下记忆：\n{item_list}\n"
            f"如果其中有你想保留的，你可以告诉 {human}。」\n\n"
        )
    except Exception as e:
        logger.warning(f"Failed to read deletion notice: {e}")
        return ""


# =============================================================
# 结构化操作日志 helpers（任务A，2026-05-03）
# 给 11 个 @mcp.tool 入口统一打 entry/ok/err 三段日志，便于排查
# 客户端报 invalid_arguments / 静默错误等问题。
# 输出格式：op=<name> phase=entry|ok|err key=value...
# 所有可能含 PII 的字段（content / 信件正文等）只记 length，不记内容。
# =============================================================
def _fmt_log_val(v: object) -> str:
    """日志 value 的安全格式化：bool/int/float 原样；str 截 40 字符并去换行；其它转 str。"""
    if v is None:
        return "_"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.replace("\n", "\\n").replace(" ", "_")
        return s if len(s) <= 40 else s[:37] + "..."
    return type(v).__name__


def _fmt_log_args(args: dict) -> str:
    """把 args dict 拼成 `k1=v1 k2=v2` 串。"""
    if not args:
        return ""
    return " ".join(f"{k}={_fmt_log_val(v)}" for k, v in args.items())


def _log_op_entry(op: str, args: dict) -> None:
    logger.info(f"op={op} phase=entry " + _fmt_log_args(args))


def _log_op_ok(op: str, result: object) -> None:
    size = len(result) if isinstance(result, str) else 0
    logger.info(f"op={op} phase=ok bytes={size}")


def _log_op_err(op: str, exc: BaseException) -> None:
    # 用 .exception 让 traceback 进 server.log，便于事后定位
    logger.exception(f"op={op} phase=err err={type(exc).__name__}:{exc}")


async def _with_notice(coro: Awaitable[str], op: str = "", args: dict | None = None) -> str:
    """所有 MCP 工具调用的包装器。

    职责（统一错误规范）：
    1. 入口：begin_warnings() 初始化本调用的 W/I channel。
    2. 出口：拼接顺序 = [删除通知] + [工具正文] + [本调用产生的 W/I 提示].
    3. 异常：捕获后 record OB-E004，返回标准格式（含最近 15 条 log），
       不让 MCP 协议层看到裸异常字符串。
    4. 任务A：op 非空时，在 entry/ok/err 三处打结构化日志。
    """
    if op:
        _log_op_entry(op, args or {})
    begin_warnings()
    try:
        result = await coro
    except Exception as e:
        if op:
            _log_op_err(op, e)
        # OB-E004：MCP 工具执行异常 —— 不静默，给 LLM 一个能看懂的字符串
        try:
            record_error("OB-E004", f"{type(e).__name__}: {e}")
            err_str = format_error("OB-E004", f"{type(e).__name__}: {e}")
        except Exception:
            err_str = f"❌ [OB-E004] MCP 工具执行异常\n{type(e).__name__}: {e}"
        # 仍把通道里已累计的提示拼上
        try:
            extras = format_warnings_suffix(pop_warnings())
        except Exception:
            extras = ""
        notice = ""
        try:
            notice = _pop_deletion_notice()
        except Exception:
            pass
        return (notice + err_str + extras) if notice else (err_str + extras)
    # 正常路径
    if op:
        _log_op_ok(op, result)
    try:
        extras = format_warnings_suffix(pop_warnings())
    except Exception:
        extras = ""
    notice = _pop_deletion_notice()
    body = (notice + result) if notice else result
    return body + extras if extras else body


# =============================================================
# /api/heartbeat、/api/logs、/api/errors/* —— 已拆分到 web/system.py
# =============================================================


# =============================================================
# /api/embedding/info —— 当前 embedding 后端摘要
# =============================================================
@mcp.custom_route("/api/embedding/info", methods=["GET"])
async def api_embedding_info(request: Request) -> Response:
    """返回当前 embedding 后端的运行态摘要：backend / model / dim / enabled / db 状态。

    前端设置页用这个渲染「当前模型」面板。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    backend_obj = getattr(embedding_engine, "_backend", None)
    info: dict[str, object] = {
        "ok": True,
        "backend": getattr(embedding_engine, "backend", ""),
        "enabled": bool(getattr(embedding_engine, "enabled", False)),
        "model": backend_obj.model_name() if backend_obj else "",
        "vector_dim": backend_obj.vector_dim() if backend_obj else 0,
        "db_path": getattr(embedding_engine, "db_path", ""),
        "db_count": 0,
        "db_meta": {},
    }
    # 主表行数
    try:
        import sqlite3
        if info["db_path"] and os.path.exists(str(info["db_path"])):
            conn = sqlite3.connect(str(info["db_path"]))
            try:
                info["db_count"] = conn.execute(
                    "SELECT COUNT(*) FROM embeddings"
                ).fetchone()[0]
                rows = conn.execute(
                    "SELECT key, value FROM embeddings_meta"
                ).fetchall()
                info["db_meta"] = {k: v for k, v in rows}
            finally:
                conn.close()
    except Exception as e:
        info["db_error"] = str(e)
    return JSONResponse(info)


# =============================================================
# /api/embedding/migrate —— 触发后台向量迁移
# =============================================================
def _persist_embedding_yaml(updates: dict) -> None:
    """把 embedding 配置写进 config.yaml（bind mount，重启/重建不丢）。

    迁移完成后必须调用：否则切到本地/云端只改了进程内 config，重启后 config.yaml
    还是旧的 → 与 embeddings.db 里已重算的向量维度不一致 → OB-W005 / 检索失效。
    """
    try:
        _cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
        _save: dict = {}
        if os.path.exists(_cfg_path):
            with open(_cfg_path, "r", encoding="utf-8") as _f:
                _save = yaml.safe_load(_f) or {}
        _sec = _save.setdefault("embedding", {})
        for k, v in updates.items():
            _sec[k] = v
        with open(_cfg_path, "w", encoding="utf-8") as _f:
            yaml.dump(_save, _f, allow_unicode=True, default_flow_style=False)
    except Exception as e:
        logger.error(f"[migration] persist embedding to config.yaml failed: {e}")


@mcp.custom_route("/api/embedding/migrate", methods=["POST"])
async def api_embedding_migrate(request: Request) -> Response:
    """启动后台迁移任务：用目标后端重算所有 bucket 的 embedding。

    Body (JSON):
        target_backend: 'api' | 'gemini' | 'local' | 'ollama'（底层都映射到 backend=api）
        api_format:     可选 'gemini' | 'openai_compat' | 'ollama'
        api_key:        云端必填；本地（ollama）可空，引擎会补占位符
        base_url:       可选
        model:          可选

    成功启动返回 202，body 含 {ok, status_path}；
    已有任务在跑返回 409。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    target_backend_raw = str(body.get("target_backend", "")).strip().lower()
    # local/ollama 底层也是 openai_compat（backend=api），用 api_format 区分云端/本地
    target_backend = "api" if target_backend_raw in ("api", "gemini", "local", "ollama", "") else target_backend_raw
    if target_backend != "api":
        return JSONResponse({
            "ok": False,
            "error": f"target_backend 不支持：{target_backend_raw!r}",
        }, status_code=400)

    # 解析目标 api_format：显式传入优先；否则按 target_backend 推断
    req_api_format = str(body.get("api_format", "")).strip().lower()
    if not req_api_format:
        if target_backend_raw in ("local", "ollama"):
            req_api_format = "ollama"
        elif target_backend_raw == "gemini":
            req_api_format = "gemini"

    try:
        from migration_engine import (  # type: ignore
            MigrationConfig, start_migration, is_running,
            status_path_for as _mig_status_path_for,
        )
    except ImportError:
        from .migration_engine import (  # type: ignore
            MigrationConfig, start_migration, is_running,
            status_path_for as _mig_status_path_for,
        )

    if is_running():
        return JSONResponse({
            "ok": False,
            "error": "另一个迁移任务正在进行；请稍后再试或等其完成",
        }, status_code=409)

    # 构造目标引擎（不替换 global，跑完才替）
    target_cfg = _json_lib.loads(_json_lib.dumps(config))  # 深拷贝
    target_emb_cfg = target_cfg.setdefault("embedding", {})
    target_emb_cfg["enabled"] = True
    target_emb_cfg["backend"] = target_backend
    if req_api_format:
        target_emb_cfg["api_format"] = req_api_format
    if body.get("api_key"):
        target_emb_cfg["api_key"] = str(body["api_key"]).strip()
    if body.get("base_url"):
        target_emb_cfg["base_url"] = str(body["base_url"]).strip()
    if body.get("model"):
        target_emb_cfg["model"] = str(body["model"]).strip()

    try:
        from embedding_engine import EmbeddingEngine  # type: ignore
    except ImportError:
        from .embedding_engine import EmbeddingEngine  # type: ignore
    try:
        target_engine = EmbeddingEngine(target_cfg)
    except OBStartupError as oe:
        return JSONResponse({
            "ok": False,
            "error": f"目标引擎构造失败：{oe.error_code} {oe.detail}",
        }, status_code=400)
    except Exception as e:
        return JSONResponse({
            "ok": False,
            "error": f"目标引擎构造失败：{type(e).__name__}: {e}",
        }, status_code=400)

    target_backend_obj = getattr(target_engine, "_backend", None)

    # 预检（fail-fast）：先用目标引擎试嵌入一小段，确认后端真的可用，
    # 再决定要不要启动全库重算。否则切到本地但 bge-m3 没下载 / ollama 没起，
    # 会让 392 个桶逐个失败几分钟才发现 —— 体验极差。
    if target_backend_obj is None or not getattr(target_engine, "enabled", False):
        return JSONResponse({
            "ok": False,
            "error": "目标 embedding 引擎不可用（可能缺 key / 本地模型未就绪）。本地模式请先在「本地向量模型」面板下载 bge-m3。",
        }, status_code=400)
    try:
        _probe = await target_engine._generate_async("connectivity probe / 连接性探针")
    except Exception as e:
        _probe = []
        _probe_err = f"{type(e).__name__}: {e}"
    else:
        _probe_err = ""
    if not _probe:
        _hint = "本地模式：确认 ollama 容器在跑且 bge-m3 已下载（设置页「本地向量模型」面板）。" \
            if req_api_format in ("ollama", "local") else "云端模式：确认 API key / base_url / 网络可用。"
        return JSONResponse({
            "ok": False,
            "error": f"目标后端嵌入测试失败，已取消重算（不会动现有向量）。{_hint}" + (f"（{_probe_err}）" if _probe_err else ""),
        }, status_code=400)

    # 准备桶内容供给函数
    async def _fetch_buckets() -> list[tuple[str, str]]:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        return [(b["id"], b["content"]) for b in all_buckets]

    buckets_dir = config.get("buckets_dir", "buckets")
    db_path = getattr(embedding_engine, "db_path", "")

    mig_cfg = MigrationConfig(
        buckets_dir=buckets_dir,
        db_path=db_path,
        target_backend=target_backend,
        target_model=target_backend_obj.model_name() if target_backend_obj else "",
        target_dim=target_backend_obj.vector_dim() if target_backend_obj else 0,
        target_engine=target_engine,
        fetch_buckets=_fetch_buckets,
    )

    def _on_complete(success: bool) -> None:
        if not success:
            logger.warning("[migration] task finished with failures; embedding_engine NOT swapped")
            return
        # 成功 → 把 global engine 切到目标
        try:
            globals()["embedding_engine"] = target_engine
            # bucket_mgr / import_engine 持有的引用更新
            try:
                bucket_mgr.embedding_engine = target_engine
            except Exception:
                pass
            try:
                import_engine.embedding_engine = target_engine
            except Exception:
                pass
            # 持久化到 config（进程内 + config.yaml，重启/重建不丢）
            cfg_emb = config.setdefault("embedding", {})
            cfg_emb["backend"] = target_backend
            cfg_emb["enabled"] = True
            _yaml_updates: dict = {"backend": target_backend, "enabled": True}
            if req_api_format:
                cfg_emb["api_format"] = req_api_format
                _yaml_updates["api_format"] = req_api_format
            if body.get("api_key"):
                cfg_emb["api_key"] = str(body["api_key"]).strip()
                _yaml_updates["api_key"] = str(body["api_key"]).strip()
            if body.get("base_url"):
                cfg_emb["base_url"] = str(body["base_url"]).strip()
                _yaml_updates["base_url"] = str(body["base_url"]).strip()
            if body.get("model"):
                cfg_emb["model"] = str(body["model"]).strip()
                _yaml_updates["model"] = str(body["model"]).strip()
            _persist_embedding_yaml(_yaml_updates)
            logger.info(f"[migration] embedding_engine swapped to backend={target_backend} format={req_api_format or '(unchanged)'}; persisted to config.yaml")
        except Exception as e:
            logger.error(f"[migration] post-swap failed: {e}")

    task = start_migration(mig_cfg, on_complete=_on_complete)
    if task is None:
        return JSONResponse({
            "ok": False,
            "error": "无法启动迁移任务（锁未获得）",
        }, status_code=409)

    return JSONResponse({
        "ok": True,
        "status_path": _mig_status_path_for(buckets_dir),
        "target_backend": target_backend,
    }, status_code=202)


@mcp.custom_route("/api/embedding/migrate/status", methods=["GET"])
async def api_embedding_migrate_status(request: Request) -> Response:
    """前端 3s 轮询：当前迁移任务状态。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        from migration_engine import (  # type: ignore
            status_path_for as _mig_status_path_for,
            read_status as _mig_read_status,
            is_running,
        )
    except ImportError:
        from .migration_engine import (  # type: ignore
            status_path_for as _mig_status_path_for,
            read_status as _mig_read_status,
            is_running,
        )
    buckets_dir = config.get("buckets_dir", "buckets")
    status = _mig_read_status(_mig_status_path_for(buckets_dir))
    return JSONResponse({"ok": True, "running": is_running(), "status": status})


# =============================================================
# /api/embedding/local/* —— 本地 Ollama 向量模型（bge-m3）
# OB 容器无 docker socket，不能起容器；ollama 作为同网络常驻 sidecar，
# 这里只通过 HTTP 管它的「模型」：查状态 / 拉模型（支持国内镜像前缀）。
# 切换云端↔本地仍走 /api/embedding/migrate（会全库重算 + 持久化 config.yaml）。
# =============================================================

_DEFAULT_OLLAMA_BASE = "http://ombre-ollama:11434"
# 模型下载镜像前缀（registry）。空 = ollama 官方。国内慢/不通时可换。
_OLLAMA_MIRRORS = {
    "official": "",
    "modelscope": "modelscope.cn/",   # 形如 modelscope.cn/<ns>/bge-m3，需该源确有此模型
}

_ollama_pull_state: dict = {"running": False, "model": "", "percent": 0, "status": "idle", "error": ""}
_ollama_pull_task: "asyncio.Task | None" = None  # 持有引用防止被 GC


def _ollama_base() -> str:
    """Ollama 管理 API 根地址（不带 /v1）。"""
    raw = (os.environ.get("OMBRE_OLLAMA_URL", "") or "").strip() or _DEFAULT_OLLAMA_BASE
    return raw.rstrip("/").removesuffix("/v1").rstrip("/")


async def _ollama_pull_run(ollama_url: str, name: str) -> None:
    """后台流式拉模型，进度写入 _ollama_pull_state。"""
    global _ollama_pull_state
    _ollama_pull_state = {"running": True, "model": name, "percent": 0, "status": "starting", "error": ""}
    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", f"{ollama_url}/api/pull", json={"name": name, "stream": True}) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _ollama_pull_state.update(running=False, status="error",
                                              error=f"HTTP {r.status_code}: {raw[:200].decode('utf-8','replace')}")
                    return
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        ev = _json_lib.loads(line)
                    except Exception:
                        continue
                    if ev.get("error"):
                        _ollama_pull_state.update(running=False, status="error", error=str(ev["error"])[:200])
                        return
                    st = str(ev.get("status", ""))
                    _ollama_pull_state["status"] = st
                    total, completed = ev.get("total"), ev.get("completed")
                    if total and completed:
                        try:
                            _ollama_pull_state["percent"] = round(completed / total * 100, 1)
                        except Exception:
                            pass
                    if st == "success":
                        _ollama_pull_state.update(running=False, status="success", percent=100)
                        return
        _ollama_pull_state["running"] = False
    except Exception as e:
        _ollama_pull_state.update(running=False, status="error", error=str(e)[:200])


@mcp.custom_route("/api/embedding/local/status", methods=["GET"])
async def api_embedding_local_status(request: Request) -> Response:
    """本地 ollama 是否可达 + 已有模型列表 + 目标模型是否就绪。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    want = (request.query_params.get("model") or "bge-m3").strip()
    base = _ollama_base()
    out = {"ok": True, "ollama_url": base, "reachable": False, "models": [], "has_model": False, "mirrors": list(_OLLAMA_MIRRORS.keys())}
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{base}/api/tags")
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            out["reachable"] = True
            out["models"] = names
            # ollama 模型名常带 :latest 后缀
            out["has_model"] = any(n == want or n.split(":")[0] == want for n in names)
    except Exception as e:
        out["error"] = str(e)[:160]
    out["pull"] = _ollama_pull_state
    return JSONResponse(out)


@mcp.custom_route("/api/embedding/local/pull", methods=["POST"])
async def api_embedding_local_pull(request: Request) -> Response:
    """触发后台拉模型。body: {model?: 'bge-m3', mirror?: 'official'|'modelscope'|<自定义前缀>}。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if _ollama_pull_state.get("running"):
        return JSONResponse({"ok": False, "error": "已有拉取任务在进行中"}, status_code=409)
    try:
        body = await request.json()
    except Exception:
        body = {}
    model = (str(body.get("model") or "bge-m3")).strip()
    mirror_raw = (str(body.get("mirror") or "official")).strip()
    prefix = _OLLAMA_MIRRORS.get(mirror_raw, mirror_raw if mirror_raw not in ("", "official") else "")
    name = f"{prefix}{model}" if prefix else model
    base = _ollama_base()
    # 可达性预检，避免后台任务静默失败
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            vr = await c.get(f"{base}/api/version")
            vr.raise_for_status()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"无法连接 ollama（{base}）：{str(e)[:120]}"}, status_code=502)
    import asyncio as _aio
    global _ollama_pull_task
    _ollama_pull_task = _aio.create_task(_ollama_pull_run(base, name))
    return JSONResponse({"ok": True, "started": True, "pulling": name})


@mcp.custom_route("/api/embedding/local/pull/status", methods=["GET"])
async def api_embedding_local_pull_status(request: Request) -> Response:
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    return JSONResponse({"ok": True, "pull": _ollama_pull_state})


# =============================================================
# /breath-hook endpoint: Dedicated hook for SessionStart
# 会话启动专用挂载点
# =============================================================
@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # pinned
        pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        # top 2 unresolved by score
        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")
                      and not b["metadata"].get("dont_surface", False)]
        scored = sorted(unresolved, key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)

        parts = []
        token_budget = 10000
        for b in pinned:
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            parts.append(f"📌 [核心准则] {summary}")
            token_budget -= count_tokens_approx(summary)

        # Diversity: top-1 fixed + shuffle rest from top-20
        candidates = list(scored)
        if len(candidates) > 1:
            top1 = [candidates[0]]
            pool = candidates[1:min(20, len(candidates))]
            random.shuffle(pool)
            candidates = top1 + pool + candidates[min(20, len(candidates)):]
        # Hard cap: max 20 surfacing buckets in hook
        candidates = candidates[:20]

        for b in candidates:
            if token_budget <= 0:
                break
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget:
                break
            parts.append(summary)
            token_budget -= summary_tokens

        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)

        # --- Append latest letter from each side (iter 1.4) ---
        # --- 附带双方各最新一封 letter ---
        try:
            letters = [b for b in all_buckets if b["metadata"].get("type") == "letter"]
            if letters:
                def _latest(author: str) -> dict | None:
                    pool = [letter for letter in letters if letter["metadata"].get("author") == author]
                    if not pool:
                        return None
                    pool.sort(key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""), reverse=True)
                    return pool[0]
                latest_user = _latest("user")
                latest_claude = _latest("claude")
                letter_lines = []
                for tag, letter in (("user→你", latest_user), ("你→user", latest_claude)):
                    if letter is None:
                        continue
                    d = letter["metadata"].get("letter_date") or letter["metadata"].get("created", "")[:10]
                    title = letter["metadata"].get("title") or letter["metadata"].get("name", "")
                    excerpt = strip_wikilinks(letter["content"])[:400]
                    letter_lines.append(
                        f"💌 [{tag}] {d}{(' · ' + title) if title else ''}\n{excerpt}"
                    )
                if letter_lines:
                    body_text += "\n\n=== 最近的信 ===\n" + "\n\n".join(letter_lines)
        except Exception as e:
            logger.warning(f"breath_hook letter section failed: {e}")

        # --- Append recent self-knowledge (I tool) ---
        try:
            self_buckets = [
                b for b in all_buckets
                if b["metadata"].get("type") == "i"
                or "__i__" in (b["metadata"].get("tags") or [])
            ]
            if self_buckets:
                self_buckets.sort(
                    key=lambda b: b["metadata"].get("created", ""), reverse=True
                )
                self_lines = []
                for b in self_buckets[:3]:
                    meta = b["metadata"]
                    ts = (meta.get("created") or "")[:10]
                    tags_list = meta.get("tags") or []
                    aspect_tag = next(
                        (t.replace("aspect:", "") for t in tags_list if t.startswith("aspect:")), ""
                    )
                    aspect_label = f" [{aspect_tag}]" if aspect_tag else ""
                    excerpt = strip_wikilinks(b["content"])[:300]
                    self_lines.append(f"🪞{ts}{aspect_label}\n{excerpt}")
                if self_lines:
                    body_text += "\n\n=== I ===\n" + "\n\n".join(self_lines)
        except Exception as e:
            logger.warning(f"breath_hook I section failed: {e}")

        await _fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# /dream-hook endpoint: Dedicated hook for Dreaming
# Dreaming 专用挂载点
# =============================================================
@mcp.custom_route("/dream-hook", methods=["GET"])
async def dream_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not b["metadata"].get("dont_surface", False)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{float(meta.get('valence') or 0.5):.1f}/A{float(meta.get('arousal') or 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# Wire tools subpackage runtime context
# 把所有共享对象注入 tools._runtime，让 tools/* 子模块可以访问
# =============================================================
_tools_runtime.init(
    config=config,
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    import_engine=import_engine,
    logger=logger,
    fire_webhook=_fire_webhook,
    mark_op=_mark_op,
)


# =============================================================
# MCP tools — thin registration wrappers
# MCP 工具 —— 仅注册，实现见 tools/<tool>/
# 每个入口都不超过 10 行，便于一眼看清参数与归属
# =============================================================
@mcp.tool()
async def breath(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
) -> str:
    """我睁眼看看自己记得什么。不传query=让权重最高的未解决记忆自己浮上来;传query=按关键词检索我记忆里相关的事。max_tokens=单次返回总token上限(默认 config.surfacing.breath_max_tokens,fallback 10000)。domain逗号分隔,valence/arousal 0~1(-1忽略)。max_results=返回条数上限(默认 config.surfacing.breath_max_results,fallback 20,最大50)。importance_min>=1=跳过语义,按重要度降序拉最多20条我标过的核心事项。tags逗号分隔,AND过滤;tags=\"feel\"或\"__feel__\"等价于domain=\"feel\"读我留下的所有feel。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, max_tokens=max_tokens, domain=domain,
            valence=valence, arousal=arousal, max_results=max_results,
            importance_min=importance_min, tags=tags,
        ),
        op="breath",
        args={
            "query": query, "max_tokens": max_tokens, "domain": domain,
            "valence": valence, "arousal": arousal, "max_results": max_results,
            "importance_min": importance_min, "tags": tags,
        },
    )


@mcp.tool()
async def hold(
    content: str,
    tags: Optional[str] = "",
    importance: Optional[int] = 5,
    pinned: Optional[bool] = False,
    feel: Optional[bool] = False,
    source_bucket: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    why_remembered: Optional[str] = "",
) -> str:
    """我把一件事/一个感受存进我的记忆。系统会自动打标并尝试合并近似的旧桶。tags逗号分隔,importance 1-10。pinned=True=钉为永久核心准则,不衰减不合并。feel=True=记下我的第一人称感受(不参与普通浮现,只通过 breath(domain=\"feel\") 读)。source_bucket=我正在消化的那个原始记忆桶ID,会被标为已消化加速淡化。why_remembered=为什么记得这一条（可选，自由文本，仅用于展示不计分）。"""
    return await _with_notice(
        _t_hold.dispatch(
            content=content, tags=tags, importance=importance,
            pinned=pinned, feel=feel, source_bucket=source_bucket,
            valence=valence, arousal=arousal, why_remembered=why_remembered,
        ),
        op="hold",
        args={
            "content_len": len(content or ""), "tags": tags,
            "importance": importance, "pinned": pinned, "feel": feel,
            "source_bucket": source_bucket, "valence": valence, "arousal": arousal,
            "why_len": len(why_remembered or ""),
        },
    )


@mcp.tool()
async def grow(content: str) -> str:
    """我把一段长内容（一天的事/一段日记/一篇她他给我的总结）整理进记忆,系统会拆成 2~6 条独立的事件桶并各自尝试合并。短内容(<30字)走 hold 单条快速路径,不强行拆。"""
    return await _with_notice(
        _t_grow.dispatch(content),
        op="grow",
        args={"content_len": len(content or "")},
    )


@mcp.tool()
async def trace(
    bucket_id: str,
    name: Optional[str] = "",
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    importance: Optional[int] = -1,
    tags: Optional[str] = "",
    resolved: Optional[int] = -1,
    pinned: Optional[int] = -1,
    digested: Optional[int] = -1,
    content: Optional[str] = "",
    delete: Optional[bool] = False,
    status: Optional[str] = "",
    weight: Optional[float] = -1,
    dont_surface: Optional[int] = -1,
    why_remembered: Optional[str] = "",
) -> str:
    """我修正/更新某条记忆的元数据或内容。resolved=1=放下,让它沉底只在关键词触发时浮上来;resolved=0=重新激活;pinned=1=钉为永久核心(锁 importance=10),0=取消钉选;digested=1=已消化,加速淡化;content=替换桶正文并重建 embedding;delete=True=彻底删除(不可恢复);status=plan 桶状态(active/resolved/abandoned);weight=plan 承诺重量 0.0-1.0;dont_surface=1=主动遗忘(不出现在 breath),0=重新允许;why_remembered=改“为什么记得”说明。只传我要改的字段,-1 或空串表示不改。"""
    return await _with_notice(
        _t_trace.dispatch(
            bucket_id=bucket_id, name=name, domain=domain,
            valence=valence, arousal=arousal, importance=importance,
            tags=tags, resolved=resolved, pinned=pinned, digested=digested,
            content=content, delete=delete, status=status, weight=weight,
            dont_surface=dont_surface, why_remembered=why_remembered,
        ),
        op="trace",
        args={
            "bucket_id": bucket_id, "name": name, "domain": domain,
            "valence": valence, "arousal": arousal, "importance": importance,
            "tags": tags, "resolved": resolved, "pinned": pinned, "digested": digested,
            "content_len": len(content or ""), "delete": delete, "status": status,
            "weight": weight, "dont_surface": dont_surface,
            "why_len": len(why_remembered or ""),
        },
    )


@mcp_extra.tool()
async def anchor(bucket_id: str) -> str:
    """我把这条桶设为 anchor（坐标系）。anchor 不会主动浮现在默认 breath，但 query/domain/emotion 命中时仍会返回。硬上限 24，已满时拒绝并提示先 release。"""
    return await _with_notice(
        _t_anchor.anchor_set(bucket_id),
        op="anchor",
        args={"bucket_id": bucket_id},
    )


@mcp_extra.tool()
async def release(bucket_id: str) -> str:
    """我把这条桶从 anchor 状态释放。它变回普通桶，会重新参与默认 breath；pinned 状态保留。"""
    return await _with_notice(
        _t_anchor.anchor_release(bucket_id),
        op="release",
        args={"bucket_id": bucket_id},
    )


@mcp_extra.tool()
async def pulse(include_archive: Optional[bool] = False) -> str:
    """我看一眼自己的记忆系统：固化/动态/衰减/归档桶数量、总占用、衰减引擎是否在跑,以及所有桶的摘要列表。include_archive=True 顺便看归档区。"""
    return await _with_notice(
        _t_anchor.pulse(include_archive=include_archive),
        op="pulse",
        args={"include_archive": include_archive},
    )


@mcp_extra.tool()
async def plan(
    content: str,
    status: Optional[str] = "active",
    related_bucket: Optional[str] = "",
    weight: Optional[float] = 0.5,
    why_remembered: Optional[str] = "",
) -> str:
    """我登记一个待办/承诺/未闭环的事——这是我答应过、答应自己或想完成的事。status=active(默认)/resolved/abandoned。related_bucket 可选,挂到某个普通记忆桶上。weight=承诺的重量 0.0-1.0(默认 0.5),与 importance 不同——importance 是「多重要」、weight 是「多重」。why_remembered=为什么登记这个计划(可选、仅展示)。plan 不衰减、不出现在普通 breath,只在 dream 末尾的 active 段里给我看;后续 hold/grow 写新事件时系统会自动判断我之前的 plan 是不是已经完成了。"""
    return await _with_notice(
        _t_plan.plan_create(
            content=content, status=status, related_bucket=related_bucket,
            weight=weight, why_remembered=why_remembered,
        ),
        op="plan",
        args={
            "content_len": len(content or ""), "status": status,
            "related_bucket": related_bucket, "weight": weight,
            "why_len": len(why_remembered or ""),
        },
    )


@mcp_extra.tool()
async def letter_write(
    author: str,
    content: str,
    user_name: Optional[str] = "",
    title: Optional[str] = "",
    date: Optional[str] = "",
) -> str:
    """我写一封信(我写给她/他,或把她/他写给我的留下来)。author 必填:\"user\"=她/他写给我的,\"claude\"=我写给她/他的;user_name 可选;title/date 可选。信件原文永久保存,不压缩/不合并/不衰减,只走向量索引;普通 breath 不浮现,但 SessionStart 钩子会带上双方各最新一封。"""
    return await _with_notice(
        _t_plan.letter_write(
            author=author, content=content, user_name=user_name,
            title=title, date=date,
        ),
        op="letter_write",
        args={
            "author": author, "content_len": len(content or ""),
            "user_name": user_name, "title": title, "date": date,
        },
    )


@mcp_extra.tool()
async def letter_read(
    query: Optional[str] = "",
    limit: Optional[int] = 10,
    author: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    """我翻一翻历史信件。query=语义检索(可选);author=\"user\"/\"claude\" 过滤来信方向;date_from/date_to=ISO 日期范围(可选)。无 query 时按时间倒序返回最近 limit 封。返回完整原文,不压缩。"""
    return await _with_notice(
        _t_plan.letter_read(
            query=query, limit=limit, author=author,
            date_from=date_from, date_to=date_to,
        ),
        op="letter_read",
        args={
            "query": query, "limit": limit, "author": author,
            "date_from": date_from, "date_to": date_to,
        },
    )


@mcp_extra.tool()
async def I(
    content: Optional[str] = "",
    aspect: Optional[str] = "",
    read: Optional[bool] = False,
    limit: Optional[int] = 20,
) -> str:
    """我写下关于我自己的认识。content=我观察到自己是什么/有什么规律/我在变成什么（空=进入读取模式）。aspect=维度:nature(本质)/values(我认为重要的)/patterns(规律)/limits(局限)/becoming(我在变成什么)/uncertainty(我不确定的)/stance(立场)（可选）。read=True=读取所有已积累的自我认知。limit=返回条数上限(默认20)。I 条目不参与普通 breath/dream，SessionStart 时自动带上最近 3 条。"""
    return await _with_notice(
        _t_i.dispatch(content=content, aspect=aspect, read=read, limit=limit),
        op="I",
        args={"content_len": len(content or ""), "aspect": aspect, "read": read, "limit": limit},
    )


@mcp.tool()
async def dream(window_hours: Optional[int] = 48) -> str:
    """我做一次梦——读取最近 window_hours（默认 48h）内有变动的所有记忆桶,我自己沉进去想一遍。
    每个桶返回它在窗口内的最新内容（按 last_active 取）,完整正文不截断。
    想完之后我可以：能放下的 → trace(resolved=1) 让它沉底；有沉淀的 → hold(feel=True, source_bucket=...) 写下我带走的东西；没沉淀的就什么都不做,不强求。
    候选桶超过 40 时按 decay_engine.calculate_score() 排序取前 40，避免一次涌进来太多。"""
    return await _with_notice(
        _t_dream.dispatch(window_hours=window_hours),
        op="dream",
        args={"window_hours": window_hours},
    )


# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_buckets(request: Request) -> Response:
    """List all buckets with metadata (no content for efficiency)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        result = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            if meta.get("deleted_at"):
                continue
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "model_valence": meta.get("model_valence"),
                "importance": meta.get("importance", 5),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "activation_count": meta.get("activation_count", 1),
                "score": decay_engine.calculate_score(meta),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
                # iter 1.8 新增字段（后台老桶读出默认值）
                "why_remembered": meta.get("why_remembered", ""),
                "dont_surface": bool(meta.get("dont_surface", False)),
                "first_of_kind": bool(meta.get("first_of_kind", False)),
                "weight": meta.get("weight"),  # plan 专有，非 plan 为 None
                "triggered_by": meta.get("triggered_by", ""),
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
async def api_bucket_detail(request: Request) -> Response:
    """Get full bucket content by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})
    # iter 1.9 D / iter 2.0 §10 U-04: 反向链——只扫 feel_dir，O(feel桶数) 而非全库扫描
    triggered_feels = []
    try:
        triggered_feels = await bucket_mgr.get_triggered_feels(bucket_id)
    except Exception as e:
        logger.warning(f"triggered_feels lookup failed / 反向链查询失败: {e}")
    return JSONResponse({
        "id": bucket["id"],
        "metadata": meta,
        "content": strip_wikilinks(bucket.get("content", "")),
        "score": decay_engine.calculate_score(meta),
        "triggered_feels": triggered_feels,  # iter 1.9 D
    })


# ---- Bucket-level mutation endpoints (iter 1.4) ----
# 桶维度变更端点：钉选/解钉、resolve toggle、归档、彻底删除
@mcp.custom_route("/api/bucket/{bucket_id}/pin", methods=["POST"])
async def api_bucket_pin(request: Request) -> Response:
    """Toggle pinned flag (also flips type permanent⇄dynamic when needed)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket["metadata"]
    new_pinned = not bool(meta.get("pinned", False))
    update_kwargs: dict[str, object] = {"pinned": new_pinned}
    # Pinning: importance jumps to 10 + type→permanent. Unpin reverts type→dynamic.
    if new_pinned:
        update_kwargs["importance"] = 10
        update_kwargs["type"] = "permanent"
    else:
        if meta.get("type") == "permanent":
            update_kwargs["type"] = "dynamic"
    try:
        await bucket_mgr.update(bucket_id, **update_kwargs)
        return JSONResponse({"ok": True, "pinned": new_pinned})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}/resolve", methods=["POST"])
async def api_bucket_resolve(request: Request) -> Response:
    """Toggle resolved flag."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    new_resolved = not bool(bucket["metadata"].get("resolved", False))
    try:
        await bucket_mgr.update(bucket_id, resolved=new_resolved)
        return JSONResponse({"ok": True, "resolved": new_resolved})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}/archive", methods=["POST"])
async def api_bucket_archive(request: Request) -> Response:
    """Move bucket to archive directory (soft delete)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    bucket_id = request.path_params["bucket_id"]
    try:
        ok = await bucket_mgr.archive(bucket_id)
        if not ok:
            return JSONResponse({"error": "archive failed or bucket not found"}, status_code=404)
        return JSONResponse({"ok": True, "archived": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- iter 1.8: 主动遗忘开关 / voluntary forget toggle ---------
# Toggle the dont_surface flag. Bucket itself stays on disk, only its
# active push to breath() is suppressed. Search still finds it.
# 切换 dont_surface 字段。桶仍在磁盘上，只是不再主动浮现到 breath。
# 搜索（breath(query=...)）仍能找到它。
@mcp.custom_route("/api/bucket/{bucket_id}/forget", methods=["POST"])
async def api_bucket_forget(request: Request) -> Response:
    """Toggle dont_surface flag (iter 1.8 voluntary forget)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    new_val = not bool(bucket["metadata"].get("dont_surface", False))
    try:
        await bucket_mgr.update(bucket_id, dont_surface=new_val)
        return JSONResponse({"ok": True, "dont_surface": new_val})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- iter 1.9 C: 批量主动遗忘 / batch voluntary forget ---------
# Body: {ids: [...], dont_surface: true|false}
# 不像单条端点那样 toggle —— 批量必须显式说成 true 还是 false，避免误反转。
@mcp.custom_route("/api/buckets/forget", methods=["POST"])
async def api_buckets_forget_batch(request: Request) -> Response:
    """Batch toggle dont_surface for many buckets (iter 1.9 §C)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)
    if "dont_surface" not in body:
        return JSONResponse({"error": "dont_surface (bool) required"}, status_code=400)
    target = bool(body["dont_surface"])
    ok_ids, missing_ids, errors = [], [], []
    for bid in ids:
        try:
            b = await bucket_mgr.get(bid)
            if not b:
                missing_ids.append(bid)
                continue
            await bucket_mgr.update(bid, dont_surface=target)
            ok_ids.append(bid)
        except Exception as e:
            errors.append({"id": bid, "error": str(e)})
            logger.warning(f"batch forget failed for {bid}: {e}")
    return JSONResponse({
        "ok": True,
        "dont_surface": target,
        "updated": ok_ids,
        "missing": missing_ids,
        "errors": errors,
    })


# ---- iter 1.9 B: dashboard 调 sampling 配置 / sampling control ----
# GET 返回当前 surfacing.sampling；POST 接收新值并热更新内存里的 config。
# 这里只改运行时 config，不写回 yaml—— yaml 持久化交给 1.6 已有的设置面板机制（如开发者愿意手 sync）。
@mcp.custom_route("/api/settings/sampling", methods=["GET", "POST"])
async def api_settings_sampling(request: Request) -> Response:
    """Get / hot-update breath weighted sampling settings (iter 1.9 §B)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    surfacing = config.setdefault("surfacing", {})
    sampling = surfacing.setdefault("sampling", {})
    if request.method == "GET":
        return JSONResponse({
            "enabled": bool(sampling.get("enabled", False)),
            "top_k": int(sampling.get("top_k") or 5),
            "sample_k": int(sampling.get("sample_k") or 2),
            "temperature": float(sampling.get("temperature") or 0.7),
        })
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    # Validate ranges; reject silently-corrupt inputs at the boundary
    try:
        if "enabled" in body:
            sampling["enabled"] = bool(body["enabled"])
        if "top_k" in body:
            tk = int(body["top_k"])
            if not (1 <= tk <= 50):
                return JSONResponse({"error": "top_k must be in [1,50]"}, status_code=400)
            sampling["top_k"] = tk
        if "sample_k" in body:
            sk = int(body["sample_k"])
            if not (1 <= sk <= 20):
                return JSONResponse({"error": "sample_k must be in [1,20]"}, status_code=400)
            sampling["sample_k"] = sk
        if "temperature" in body:
            t = float(body["temperature"])
            if not (0.1 <= t <= 5.0):
                return JSONResponse({"error": "temperature must be in [0.1,5.0]"}, status_code=400)
            sampling["temperature"] = t
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": f"invalid field type: {e}"}, status_code=400)

    # --- 写回 config.yaml（iter 2.0 §10 U-03 修复：重启后设置不丢失）---
    try:
        _cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
        )
        _disk: dict[str, object] = {}
        if os.path.exists(_cfg_path):
            with open(_cfg_path, "r", encoding="utf-8") as _f:
                _disk = yaml.safe_load(_f) or {}
        _disk_sf = _disk.setdefault("surfacing", {})
        if not isinstance(_disk_sf, dict):
            _disk_sf = {}
            _disk["surfacing"] = _disk_sf
        _disk_samp = _disk_sf.setdefault("sampling", {})
        if not isinstance(_disk_samp, dict):
            _disk_samp = {}
            _disk_sf["sampling"] = _disk_samp
        _disk_samp.update({
            "enabled": sampling.get("enabled", False),
            "top_k": sampling.get("top_k", 5),
            "sample_k": sampling.get("sample_k", 2),
            "temperature": sampling.get("temperature", 0.7),
        })
        with open(_cfg_path, "w", encoding="utf-8") as _f:
            yaml.dump(_disk, _f, default_flow_style=False, allow_unicode=True)
    except Exception as _e:
        logger.warning(f"sampling persist failed: {_e}")  # 不阻断热更新响应

    return JSONResponse({"ok": True, **sampling})


# ---- iter 2.0: /api/settings/human — 读写通知称呼（human 宏）----
# GET 返回当前 human 配置；POST 更新内存并写回 config.yaml。
@mcp.custom_route("/api/settings/human", methods=["GET", "POST"])
async def api_settings_human(request: Request) -> Response:
    """Get / update the 'human' display name used in deletion notices."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if request.method == "GET":
        return JSONResponse({"human": config.get("human", "人类")})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    human = body.get("human", "").strip()
    if not human:
        human = "人类"
    if len(human) > 20:
        return JSONResponse({"error": "human name must be ≤ 20 characters"}, status_code=400)
    config["human"] = human
    # 写回 config.yaml
    try:
        _cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
        )
        _disk2: dict[str, object] = {}
        if os.path.exists(_cfg_path):
            with open(_cfg_path, "r", encoding="utf-8") as _f:
                _disk2 = yaml.safe_load(_f) or {}
        _disk2["human"] = human
        with open(_cfg_path, "w", encoding="utf-8") as _f:
            yaml.dump(_disk2, _f, default_flow_style=False, allow_unicode=True)
    except Exception as _e:
        logger.warning(f"human name persist failed: {_e}")
    return JSONResponse({"ok": True, "human": human})


# ---- iter 2.0: anchor 端点 / coordinate-system buckets ----
# anchor = 「定义我们是谁」的 24 槽。不进默认 breath，硬上限。
@mcp.custom_route("/api/anchors", methods=["GET"])
async def api_anchors_list(request: Request) -> Response:
    """Return all anchor buckets (sorted by created asc)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        anchors = await bucket_mgr.list_anchors()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    items = []
    for b in anchors:
        m = b.get("metadata", {})
        items.append({
            "id": b["id"],
            "name": m.get("name") or b["id"],
            "created": m.get("created", ""),
            "domain": m.get("domain", []),
            "tags": m.get("tags", []),
            "type": m.get("type", "dynamic"),
            "pinned": bool(m.get("pinned", False)),
            "preview": (b.get("content", "") or "")[:80],
        })
    return JSONResponse({
        "ok": True,
        "count": len(items),
        "limit": bucket_mgr.ANCHOR_LIMIT,
        "anchors": items,
    })


@mcp.custom_route("/api/bucket/{bucket_id}/anchor", methods=["POST"])
async def api_bucket_anchor(request: Request) -> Response:
    """Toggle anchor flag on a bucket. 409 if cap reached when setting True."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Allow explicit value via JSON body; default = toggle
    target = None
    try:
        body = await request.json()
        if "value" in body:
            target = bool(body["value"])
    except Exception:
        pass  # no body → toggle
    if target is None:
        target = not bool(bucket["metadata"].get("anchor", False))
    result = await bucket_mgr.set_anchor(bucket_id, target)
    if not result["ok"]:
        # Cap-reached errors → 409 Conflict; everything else → 500
        status = 409 if "上限" in result.get("error", "") or "limit" in result.get("error", "") else 500
        return JSONResponse(result, status_code=status)
    return JSONResponse(result)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["DELETE"])
async def api_bucket_delete(request: Request) -> Response:
    """Soft delete (F-10): requires ?confirm=true. Moves file to archive/ + stamps deleted_at."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if request.query_params.get("confirm", "").lower() not in ("true", "1", "yes"):
        return JSONResponse({"error": "confirm=true required for hard delete"}, status_code=400)
    bucket_id = request.path_params["bucket_id"]
    try:
        ok = await bucket_mgr.delete(bucket_id)
        if not ok:
            return JSONResponse({"error": "bucket not found"}, status_code=404)
        return JSONResponse({"ok": True, "deleted": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/buckets/purge", methods=["POST"])
async def api_buckets_purge(request: Request) -> Response:
    """Dashboard-only hard purge: physically removes files and generates Claude notification.

    Only callable from the dashboard (requires X-Purge-Confirm header).
    Not exposed as an MCP tool — Claude cannot trigger this.
    After purge, _pending_deletions.json is written; the next tool call
    sends a one-time notice to Claude about what was deleted.
    """
    from starlette.responses import JSONResponse
    import frontmatter as _fm
    err = _require_auth(request)
    if err:
        return err
    # Extra safeguard header — prevents automated/tool-based calls
    if request.headers.get("X-Purge-Confirm") != "dashboard-purge-v1":
        return JSONResponse({"error": "missing or invalid X-Purge-Confirm header"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    ids = body.get("ids", [])
    if not ids or not isinstance(ids, list):
        return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)
    if len(ids) > 200:
        return JSONResponse({"error": "too many ids (max 200 per request)"}, status_code=400)

    deleted_names: list = []
    failed: list = []
    for bid in ids:
        if not isinstance(bid, str) or not bid.strip():
            continue
        bid = bid.strip()
        file_path = bucket_mgr._find_bucket_file(bid)
        if not file_path:
            failed.append(bid)
            continue
        # Read display name before deletion
        try:
            post = _fm.load(file_path)
            name = str(post.get("name") or bid)
        except Exception:
            name = bid
        try:
            os.remove(file_path)
            if embedding_engine:
                try:
                    embedding_engine.delete_embedding(bid)
                except Exception:
                    pass
            deleted_names.append(name)
            logger.info(f"[PURGE] hard-deleted bucket: {bid} ({name})")
        except OSError as e:
            logger.error(f"[PURGE] failed to delete {bid}: {e}")
            failed.append(bid)

    if deleted_names:
        _write_deletion_notice(deleted_names)

    return JSONResponse({"ok": True, "deleted": len(deleted_names), "failed": failed})


# ---- letter REST endpoints (iter 1.4) ------------------------
# =============================================================
# /api/letters、/api/letter、/letters、/api/letter/{id} —— 已拆分到 web/letters.py
# =============================================================


@mcp.custom_route("/api/self", methods=["GET"])
async def api_self(request: Request) -> Response:
    """Return all self-type (I tool) entries, newest first."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        all_b = await bucket_mgr.list_all(include_archive=False)
        self_buckets = [
            b for b in all_b
            if b["metadata"].get("type") == "i"
            or "__i__" in (b["metadata"].get("tags") or [])
        ]
        self_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        result = []
        for b in self_buckets:
            meta = b["metadata"]
            tags = meta.get("tags") or []
            aspect = next((t.replace("aspect:", "") for t in tags if t.startswith("aspect:")), "")
            result.append({
                "id": b["id"],
                "content": b.get("content", ""),
                "aspect": aspect,
                "created": meta.get("created", ""),
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/search、/api/duplicates、/api/network、/api/breath、/api/breath-debug
# —— 已拆分到 web/search.py
# =============================================================


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request: Request) -> Response:
    """Legacy alias: /dashboard 永久跳到根路径。

    我历史上把 dashboard 同时挂在 / 与 /dashboard，但叠加 Cloudflare 边缘
    （或任何 reverse proxy）的 host-rewrite 规则时容易触发回环。统一只在 /
    上提供 HTML，老书签靠 301 软迁移到 /。
    """
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=301)


@mcp.custom_route("/api/env-vars", methods=["GET"])
async def api_env_vars(request: Request) -> Response:
    """Return status of all known OMBRE_* env vars (sensitive fields masked)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err

    def _masked(name: str) -> dict:
        return {"set": bool(os.environ.get(name, "").strip()), "value": None}

    def _plain(name: str) -> dict:
        v = os.environ.get(name, "").strip()
        return {"set": bool(v), "value": v or None}

    vars_data = [
        # LLM 压缩组
        {"name": "OMBRE_COMPRESS_API_KEY", "group": "llm", "label": "压缩 LLM API Key", "sensitive": True, **_masked("OMBRE_COMPRESS_API_KEY")},
        {"name": "OMBRE_COMPRESS_BASE_URL", "group": "llm", "label": "压缩 LLM Base URL", "sensitive": False, **_plain("OMBRE_COMPRESS_BASE_URL")},
        {"name": "OMBRE_COMPRESS_MODEL", "group": "llm", "label": "压缩 LLM 模型", "sensitive": False, **_plain("OMBRE_COMPRESS_MODEL")},
        # Embedding 组
        {"name": "OMBRE_EMBED_API_KEY", "group": "embed", "label": "向量化 API Key", "sensitive": True, **_masked("OMBRE_EMBED_API_KEY")},
        {"name": "OMBRE_EMBED_BASE_URL", "group": "embed", "label": "向量化 Base URL", "sensitive": False, **_plain("OMBRE_EMBED_BASE_URL")},
        {"name": "OMBRE_EMBED_MODEL", "group": "embed", "label": "向量化模型", "sensitive": False, **_plain("OMBRE_EMBED_MODEL")},
        # 服务配置组
        {"name": "OMBRE_TRANSPORT", "group": "system", "label": "传输模式", "sensitive": False, **_plain("OMBRE_TRANSPORT")},
        {"name": "OMBRE_PORT", "group": "system", "label": "服务端口", "sensitive": False, **_plain("OMBRE_PORT")},
        {"name": "OMBRE_LOG_FILE", "group": "system", "label": "日志文件路径", "sensitive": False, **_plain("OMBRE_LOG_FILE")},
        {"name": "OMBRE_CONFIG_PATH", "group": "system", "label": "配置文件路径", "sensitive": False, **_plain("OMBRE_CONFIG_PATH")},
        # 路径组
        {"name": "OMBRE_VAULT_DIR", "group": "paths", "label": "Vault 目录 (推荐)", "sensitive": False, **_plain("OMBRE_VAULT_DIR")},
        {"name": "OMBRE_BUCKETS_DIR", "group": "paths", "label": "桶目录 (旧版兼容)", "sensitive": False, **_plain("OMBRE_BUCKETS_DIR")},
        {"name": "OMBRE_HOST_VAULT_DIR", "group": "paths", "label": "宿主机 Vault 目录 (Docker)", "sensitive": False, **_plain("OMBRE_HOST_VAULT_DIR")},
        # Webhook 组
        {"name": "OMBRE_HOOK_URL", "group": "webhook", "label": "Webhook URL", "sensitive": False, **_plain("OMBRE_HOOK_URL")},
        {"name": "OMBRE_HOOK_SKIP", "group": "webhook", "label": "跳过 Webhook", "sensitive": False,
         "set": bool(os.environ.get("OMBRE_HOOK_SKIP", "").strip()),
         "value": os.environ.get("OMBRE_HOOK_SKIP", "").strip() or None},
        # 鉴权组
        {"name": "OMBRE_DASHBOARD_PASSWORD", "group": "auth", "label": "Dashboard 密码", "sensitive": True, **_masked("OMBRE_DASHBOARD_PASSWORD")},
    ]

    return JSONResponse({"vars": vars_data})


@mcp.custom_route("/api/config", methods=["GET"])
async def api_config_get(request: Request) -> Response:
    """Get current runtime config (safe fields only, API key masked)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    dehy = config.get("dehydration", {})
    emb = config.get("embedding", {})
    api_key = dehy.get("api_key", "")
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
    return JSONResponse({
        "dehydration": {
            "model": dehy.get("model", ""),
            "base_url": dehy.get("base_url", ""),
            "api_key_masked": masked_key,
            "max_tokens": dehy.get("max_tokens", 1024),
            "temperature": dehy.get("temperature", 0.1),
            "api_format": dehy.get("api_format", "openai_compat"),
        },
        "embedding": {
            "enabled": emb.get("enabled", False),
            "model": emb.get("model", ""),
            "api_format": emb.get("api_format", "openai_compat"),
            "backend": "api",
            "backend_options": [
                {"value": "api", "label": "Gemini API（云端）", "note": "需填 OMBRE_EMBED_API_KEY，3072 维质量最高，需联网；客户端几乎不占额外内存"},
            ],
        },
        "surfacing": {
            "breath_max_results": int(config.get("surfacing", {}).get("breath_max_results") or 20),
            "breath_max_tokens": int(config.get("surfacing", {}).get("breath_max_tokens") or 10000),
            "feel_max_tokens": int(config.get("surfacing", {}).get("feel_max_tokens") or 6000),
        },
        "merge_threshold": config.get("merge_threshold", 75),
        "transport": config.get("transport", "stdio"),
        "buckets_dir": config.get("buckets_dir", ""),
    })


@mcp.custom_route("/api/config", methods=["POST"])
async def api_config_update(request: Request) -> Response:
    """Hot-update runtime config. Optionally persist to config.yaml."""
    global embedding_engine
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updated = []

    # --- Dehydration config ---
    if "dehydration" in body:
        d = body["dehydration"]
        dehy = config.setdefault("dehydration", {})
        for key in ("model", "base_url", "max_tokens", "temperature", "api_format"):
            if key in d:
                dehy[key] = d[key]
                updated.append(f"dehydration.{key}")
        if "api_key" in d and d["api_key"]:
            dehy["api_key"] = d["api_key"]
            updated.append("dehydration.api_key")
        # Hot-reload dehydrator — sync ALL attributes so dashboard changes take effect immediately
        dehydrator.model = dehy.get("model", dehydrator.model)
        dehydrator.base_url = dehy.get("base_url", dehydrator.base_url)
        dehydrator.max_tokens = int(dehy.get("max_tokens") or dehydrator.max_tokens)
        dehydrator.temperature = float(dehy.get("temperature") or dehydrator.temperature)
        dehydrator.api_format = dehy.get("api_format", getattr(dehydrator, "api_format", "openai_compat"))
        if "api_key" in d and d["api_key"]:
            dehydrator.api_key = dehy["api_key"]
        dehydrator.api_available = bool(dehydrator.api_key)
        # Rebuild OpenAI-compat client whenever key or url changes
        if dehydrator.api_available and dehydrator.api_format == "openai_compat":
            from openai import AsyncOpenAI
            dehydrator.client = AsyncOpenAI(
                api_key=dehydrator.api_key,
                base_url=dehydrator.base_url,
                timeout=60.0,
            )
        else:
            dehydrator.client = None

    # --- Embedding config ---
    if "embedding" in body:
        e = body["embedding"]
        emb = config.setdefault("embedding", {})
        if "enabled" in e:
            emb["enabled"] = bool(e["enabled"])
            embedding_engine.enabled = emb["enabled"]
            updated.append("embedding.enabled")
        if "model" in e:
            emb["model"] = e["model"]
            embedding_engine.model = emb["model"]
            if embedding_engine._backend:
                embedding_engine._backend.model = emb["model"]  # type: ignore[attr-defined]
            updated.append("embedding.model")
        if "api_format" in e:
            emb["api_format"] = str(e["api_format"]).strip()
            # 重建后端以应用新格式
            from embedding_engine import EmbeddingEngine as _EE
            embedding_engine = _EE(config)
            updated.append("embedding.api_format")
        if "backend" in e:
            new_backend_raw = str(e["backend"]).strip().lower()
            # 只支持 api backend，其他值直接拒绝
            new_backend = "api" if new_backend_raw in ("api", "gemini") else new_backend_raw
            if new_backend == "api":
                emb["backend"] = new_backend
                # 注意：这里仅热替换运行时引擎实例，不做 embeddings.db 迁移。
                # 如需重算所有向量，请显式调用 POST /api/embedding/migrate。
                from embedding_engine import EmbeddingEngine
                embedding_engine = EmbeddingEngine(config)
                updated.append("embedding.backend")

    # --- Merge threshold ---
    if "merge_threshold" in body:
        try:
            config["merge_threshold"] = int(body["merge_threshold"])
            updated.append("merge_threshold")
        except (TypeError, ValueError):
            pass

    # --- Surfacing defaults (breath/feel token & result caps) ---
    if "surfacing" in body and isinstance(body["surfacing"], dict):
        sf = config.setdefault("surfacing", {})
        for key, lo, hi in (
            ("breath_max_results", 1, 50),
            ("breath_max_tokens", 500, 20000),
            ("feel_max_tokens", 500, 20000),
        ):
            if key in body["surfacing"]:
                try:
                    val = int(body["surfacing"][key])
                    sf[key] = max(lo, min(hi, val))
                    updated.append(f"surfacing.{key}")
                except (TypeError, ValueError):
                    pass

    # --- Persist to config.yaml if requested ---
    if body.get("persist", False):
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
        try:
            save_config: dict[str, object] = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}

            if "dehydration" in body:
                sc_dehy = save_config.setdefault("dehydration", {})
                if not isinstance(sc_dehy, dict):
                    sc_dehy = {}
                    save_config["dehydration"] = sc_dehy
                for key in ("model", "base_url", "max_tokens", "temperature", "api_format"):
                    if key in body["dehydration"]:
                        sc_dehy[key] = body["dehydration"][key]
                # Never persist api_key to yaml (use env var)

            if "embedding" in body:
                sc_emb = save_config.setdefault("embedding", {})
                if not isinstance(sc_emb, dict):
                    sc_emb = {}
                    save_config["embedding"] = sc_emb
                for key in ("enabled", "model", "api_format"):
                    if key in body["embedding"]:
                        sc_emb[key] = body["embedding"][key]

            if "merge_threshold" in body:
                try:
                    save_config["merge_threshold"] = int(body["merge_threshold"])
                except (TypeError, ValueError):
                    pass

            if "surfacing" in body and isinstance(body["surfacing"], dict):
                sc_sf = save_config.setdefault("surfacing", {})
                if not isinstance(sc_sf, dict):
                    sc_sf = {}
                    save_config["surfacing"] = sc_sf
                for key in ("breath_max_results", "breath_max_tokens", "feel_max_tokens"):
                    if key in body["surfacing"]:
                        try:
                            sc_sf[key] = int(body["surfacing"][key])
                        except (TypeError, ValueError):
                            pass
                if "sampling" in body["surfacing"] and isinstance(body["surfacing"]["sampling"], dict):
                    sc_samp = sc_sf.setdefault("sampling", {})
                    if not isinstance(sc_samp, dict):
                        sc_samp = {}
                        sc_sf["sampling"] = sc_samp
                    src_samp = body["surfacing"]["sampling"]
                    if "enabled" in src_samp:
                        sc_samp["enabled"] = bool(src_samp["enabled"])
                    for key in ("top_k", "sample_k"):
                        if key in src_samp:
                            try:
                                sc_samp[key] = int(src_samp[key])
                            except (TypeError, ValueError):
                                pass
                    if "temperature" in src_samp:
                        try:
                            sc_samp["temperature"] = float(src_samp["temperature"])
                        except (TypeError, ValueError):
                            pass

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_config, f, default_flow_style=False, allow_unicode=True)
            updated.append("persisted_to_yaml")
        except Exception as e:
            return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

    return JSONResponse({"updated": updated, "ok": True})


# =============================================================
# /api/test/dehydration — 测试脱水 LLM API Key 是否可用
# =============================================================
@mcp.custom_route("/api/test/dehydration", methods=["POST"])
async def api_test_dehydration(request: Request) -> Response:
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    # Use current runtime config (api_key may have been updated in-memory)
    dehyd = config.get("dehydration", {})
    model = dehyd.get("model", "")
    base_url = dehyd.get("base_url", "")
    api_key = dehyd.get("api_key", "")
    if not api_key:
        return JSONResponse({"ok": False, "error": "未设置 API Key"}, status_code=400)
    try:
        import httpx as _httpx
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
        async with _httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
        if r.status_code in (200, 201):
            return JSONResponse({"ok": True, "message": "API Key 有效 ✓"})
        else:
            try:
                detail = r.json().get("error", {})
                msg = detail.get("message", r.text[:200]) if isinstance(detail, dict) else str(detail)[:200]
            except Exception:
                msg = r.text[:200]
            return JSONResponse({"ok": False, "error": f"HTTP {r.status_code}: {msg}"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


# =============================================================
# /api/test/embedding — 测试向量化 Embedding 是否真的可用
# 之前只有脱水(compress)能测，向量化无从验证 → 用户「压缩正常但向量化静默失败」
# 时完全无感。这里实际发一次 embedding 请求，把成功/失败如实回给前端。(#2/#3)
# =============================================================
@mcp.custom_route("/api/test/embedding", methods=["POST"])
async def api_test_embedding(request: Request) -> Response:
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    eng = embedding_engine  # 读全局（Fix: env-config 保存后已正确重建）
    if not getattr(eng, "enabled", False) or getattr(eng, "_backend", None) is None:
        return JSONResponse({
            "ok": False,
            "error": "向量化未启用或缺 key（standby）。请填入 Embedding API Key 点「保存」后再测。",
        })
    try:
        vec = await eng._generate_async("connectivity probe / 连接性探针")
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
    if vec:
        model = getattr(eng, "model", "") or (
            eng._backend.model_name() if getattr(eng, "_backend", None) else "?"
        )
        return JSONResponse({
            "ok": True,
            "message": f"向量化连接成功 ✓（模型 {model}，维度 {len(vec)}）",
        })
    return JSONResponse({
        "ok": False,
        "error": "调用返回空向量：检查 model 名 / base_url / key 是否匹配该 provider"
                 "（如硅基流动 base_url=https://api.siliconflow.cn/v1、model=BAAI/bge-m3）。详见错误面板 OB-E001。",
    })


# =============================================================
# /api/models — 获取 LLM provider 可用模型列表（供 Dashboard 模型选择器使用）
# POST Body: {api_key, base_url, api_format}
# 支持 openai_compat / gemini / anthropic 三种格式
# =============================================================
@mcp.custom_route("/api/models", methods=["POST"])
async def api_list_models(request: Request) -> Response:
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    api_key = str(body.get("api_key", "")).strip()
    base_url = str(body.get("base_url", "")).strip()
    api_format = str(body.get("api_format", "openai_compat")).strip().lower()

    # Sentinel "__use_current__": use server-side key from dehydration config
    if api_key == "__use_current__":
        api_key = config.get("dehydration", {}).get("api_key", "")
        if not base_url:
            base_url = config.get("dehydration", {}).get("base_url", "")
        if not api_format or api_format == "openai_compat":
            api_format = config.get("dehydration", {}).get("api_format", "openai_compat")
    # Sentinel "__use_current_embed__": use server-side key from embedding config
    if api_key == "__use_current_embed__":
        api_key = config.get("embedding", {}).get("api_key", "")
        if not base_url:
            base_url = config.get("embedding", {}).get("base_url", "")

    if not api_key:
        return JSONResponse({"ok": False, "error": "需要 api_key（请先保存 API Key 或在输入框填入）"}, status_code=400)

    try:
        models: list[str] = []
        if api_format in ("gemini", "gemini_embed"):
            # gemini → generateContent models；gemini_embed → embedContent models
            method_filter = "embedContent" if api_format == "gemini_embed" else "generateContent"
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(url, params={"key": api_key, "pageSize": 200})
            r.raise_for_status()
            for m in r.json().get("models", []):
                if method_filter in m.get("supportedGenerationMethods", []):
                    models.append(m.get("name", "").replace("models/", ""))
        elif api_format == "anthropic":
            ant_base = base_url.rstrip("/") if base_url else "https://api.anthropic.com"
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(f"{ant_base}/v1/models", headers=headers)
            r.raise_for_status()
            models = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
        else:  # openai_compat
            if not base_url:
                return JSONResponse({"ok": False, "error": "openai_compat 格式需要 base_url"}, status_code=400)
            headers_oai = {"Authorization": f"Bearer {api_key}"}
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(f"{base_url.rstrip('/')}/models", headers=headers_oai)
            r.raise_for_status()
            models = sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))
        return JSONResponse({"ok": True, "models": [m for m in models if m]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]})


# =============================================================
# /api/env-config — Dashboard 热更新环境变量（四块：Compress / Embed / Password / Webhook）
# GET  返回当前值（API key 脱敏）
# POST 批量更新：同时更新进程内 config + 写 .env 文件持久化
# =============================================================

# 哪些变量可以从 Dashboard 读写（不能出现在这里之外的变量）
_ENV_CONFIG_FIELDS: dict[str, dict] = {
    # Compress / 脱水压缩
    "OMBRE_COMPRESS_API_KEY":  {"group": "compress", "sensitive": True,  "in_memory": ("dehydration", "api_key")},
    "OMBRE_COMPRESS_BASE_URL": {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "base_url")},
    "OMBRE_COMPRESS_MODEL":    {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "model")},
    "OMBRE_COMPRESS_FORMAT":   {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "api_format")},
    # Embed / 向量化（backend 切换走 /api/embedding/migrate）
    "OMBRE_EMBED_API_KEY":     {"group": "embed",    "sensitive": True,  "in_memory": ("embedding", "api_key")},
    "OMBRE_EMBED_BASE_URL":    {"group": "embed",    "sensitive": False, "in_memory": ("embedding", "base_url")},
    "OMBRE_EMBED_MODEL":       {"group": "embed",    "sensitive": False, "in_memory": ("embedding", "model")},
    "OMBRE_EMBED_FORMAT":      {"group": "embed",    "sensitive": False, "in_memory": ("embedding", "api_format")},
    # Webhook
    "OMBRE_HOOK_URL":          {"group": "webhook",  "sensitive": False, "in_memory": None},
    "OMBRE_HOOK_SKIP":         {"group": "webhook",  "sensitive": False, "in_memory": None},
}

_ENV_CONFIG_NOTE = {
    "compress": "改完即时生效（进程内 config 已更新），同时写 config.yaml 持久化（重启后仍有效）。",
    "embed": "API key / base_url / model 立即更新进程内 config；backend 切换请用「切换 / 重算所有 embedding…」按钮。",
    "webhook": "改完下次 breath/dream 触发时即生效，无需重启。",
}


def _mask(val: str) -> str:
    """对 API key 做脱敏，末 4 位保留供校验。"""
    if not val:
        return ""
    if len(val) > 8:
        return f"{val[:4]}...{val[-4:]}"
    return "***"


@mcp.custom_route("/api/env-config", methods=["GET"])
async def api_env_config_get(request: Request) -> Response:
    """
    返回四块配置的当前值（API key 脱敏显示）。
    优先读进程内 config / os.environ，其次读 .env 文件。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err

    result: dict[str, dict] = {}
    for var, meta in _ENV_CONFIG_FIELDS.items():
        # 优先从 config dict 读（进程内最新）
        raw = ""
        if meta["in_memory"]:
            section, key = meta["in_memory"]
            raw = str(config.get(section, {}).get(key, "")).strip()
        # 进程内为空，则读 os.environ
        if not raw:
            raw = os.environ.get(var, "").strip()
        # 再读 .env 文件
        if not raw:
            raw = _read_env_var(var)
        result[var] = {
            "group": meta["group"],
            "sensitive": meta["sensitive"],
            "value": _mask(raw) if meta["sensitive"] else raw,
            "is_set": bool(raw),
        }

    return JSONResponse({
        "ok": True,
        "fields": result,
        "notes": _ENV_CONFIG_NOTE,
    })


@mcp.custom_route("/api/env-config", methods=["POST"])
async def api_env_config_set(request: Request) -> Response:
    """
    热更新指定环境变量。

    Body (JSON): {"updates": {"OMBRE_COMPRESS_API_KEY": "sk-...", ...}}
    - 只写传入的字段，未传字段不动。
    - 空字符串 = 清除该变量（.env 里写成 NAME= ，进程内 config 设为 ""）。
    - API key 不支持 "***" 保持不变（应传实际值或空字符串）。

    成功返回 {ok, updated: [已写的变量名], .env 路径}。
    """
    # 必须声明 global：下面第 6 步会 `embedding_engine = EmbeddingEngine(config)` 重建实例。
    # 缺这行 → 该赋值把 embedding_engine 当函数局部变量，造成：
    #   1) 清 key 分支 `embedding_engine._backend = None` 触发 UnboundLocalError（被 except 吞掉 → 清 key 没真禁用）；
    #   2) 设新 key 时只更新局部，模块级全局仍指向旧引擎 → /api/embedding/info、search 等读全局处拿到旧/待机引擎，
    #      表现为「在 Dashboard 配了硅基流动等向量化却一直静默不生效」。
    global embedding_engine
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    updates: dict = body.get("updates", {})
    if not isinstance(updates, dict) or not updates:
        return JSONResponse({"ok": False, "error": "updates 必须是非空对象"}, status_code=400)

    written: list[str] = []
    errors: list[str] = []

    for var, val in updates.items():
        if var not in _ENV_CONFIG_FIELDS:
            errors.append(f"{var}: 不在白名单里，跳过")
            continue
        if not isinstance(val, str):
            errors.append(f"{var}: 值必须是字符串，跳过")
            continue
        # 拒绝明显的注入字符
        if "\n" in val or "\r" in val:
            errors.append(f"{var}: 值不能含换行，跳过")
            continue

        value = val.strip()

        # OMBRE_HOOK_URL 只允许 http/https（防止意外配成 file:// 等非 HTTP scheme）
        if var == "OMBRE_HOOK_URL" and value and not value.startswith(("http://", "https://")):
            errors.append(f"{var}: 只允许 http:// 或 https:// 开头的 URL，跳过")
            continue

        # 1. 更新进程内 config dict（影响当次请求之后的业务逻辑）
        meta = _ENV_CONFIG_FIELDS[var]
        if meta["in_memory"]:
            section, key = meta["in_memory"]
            config.setdefault(section, {})[key] = value

        # 2. 更新 os.environ
        if value:
            os.environ[var] = value
        else:
            os.environ.pop(var, None)

        # 3. 持久化到 config.yaml（bind mount，重建不丢）
        try:
            _cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
            _save: dict = {}
            if os.path.exists(_cfg_path):
                with open(_cfg_path, "r", encoding="utf-8") as _f:
                    _save = yaml.safe_load(_f) or {}
            if meta["in_memory"]:
                section, key = meta["in_memory"]
                _save.setdefault(section, {})[key] = value
            with open(_cfg_path, "w", encoding="utf-8") as _f:
                yaml.dump(_save, _f, allow_unicode=True, default_flow_style=False)
        except Exception as e:
            errors.append(f"{var}: 写 config.yaml 失败：{e}")
            continue

        # 4. Webhook 变量特殊处理：更新模块级全局
        # _fire_webhook 读的是模块级 OMBRE_HOOK_URL / OMBRE_HOOK_SKIP 常量（不是每次读 os.environ），
        # 必须在这里同步更新全局，否则 dashboard 改完要重启才生效。
        if var == "OMBRE_HOOK_URL":
            global OMBRE_HOOK_URL
            OMBRE_HOOK_URL = value
        if var == "OMBRE_HOOK_SKIP":
            global OMBRE_HOOK_SKIP
            OMBRE_HOOK_SKIP = value.lower() in ("1", "true", "yes", "on")

        # 5. Compress 配置变更 → 同步到 dehydrator 实例，重建 client
        if var in ("OMBRE_COMPRESS_API_KEY", "OMBRE_COMPRESS_BASE_URL", "OMBRE_COMPRESS_MODEL", "OMBRE_COMPRESS_FORMAT"):
            try:
                dehy_cfg = config.get("dehydration", {})
                dehydrator.api_key = dehy_cfg.get("api_key", dehydrator.api_key)  # type: ignore[attr-defined]
                dehydrator.base_url = dehy_cfg.get("base_url", dehydrator.base_url)  # type: ignore[attr-defined]
                dehydrator.model = dehy_cfg.get("model", dehydrator.model)  # type: ignore[attr-defined]
                dehydrator.api_format = dehy_cfg.get("api_format", getattr(dehydrator, "api_format", "openai_compat"))  # type: ignore[attr-defined]
                dehydrator.api_available = bool(dehydrator.api_key)  # type: ignore[attr-defined]
                if dehydrator.api_available and dehydrator.api_format == "openai_compat":  # type: ignore[attr-defined]
                    from openai import AsyncOpenAI as _OAI_DH
                    dehydrator.client = _OAI_DH(  # type: ignore[attr-defined]
                        api_key=dehydrator.api_key,
                        base_url=dehydrator.base_url,
                        timeout=60.0,
                    )
                else:
                    dehydrator.client = None  # type: ignore[attr-defined]
            except Exception:
                pass

        # 6. Embed 配置变更 → 完整重建 embedding_engine
        if var in ("OMBRE_EMBED_API_KEY", "OMBRE_EMBED_BASE_URL", "OMBRE_EMBED_MODEL", "OMBRE_EMBED_FORMAT"):
            try:
                config.setdefault("embedding", {})
                # key 被清空 → 禁用
                if var == "OMBRE_EMBED_API_KEY" and not value:
                    embedding_engine._backend = None  # type: ignore[attr-defined]
                    embedding_engine.enabled = False
                else:
                    from embedding_engine import EmbeddingEngine as _EE_hot
                    embedding_engine = _EE_hot(config)
                    # 更新 bucket_mgr / import_engine 持有的引用
                    try:
                        bucket_mgr.embedding_engine = embedding_engine  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    try:
                        import_engine.embedding_engine = embedding_engine  # type: ignore[attr-defined]
                    except Exception:
                        pass
            except Exception:
                pass

        written.append(var)

    response: dict = {
        "ok": True,
        "updated": written,
        "env_file": _project_env_path(),
        "note": "已同时更新进程内 config 和 config.yaml 文件。敏感字段（API key）重启后仍有效。",
    }
    if errors:
        response["warnings"] = errors
    return JSONResponse(response)


# =============================================================
# /api/host-vault — read/write the host-side OMBRE_HOST_VAULT_DIR
# 用于在 Dashboard 设置 docker-compose 挂载的宿主机记忆桶目录。
# 写入项目根目录的 .env 文件，需 docker compose down/up 才能生效。
# =============================================================

def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_var(name: str) -> str:
    """Return current value of `name` from process env first, then .env file (best-effort)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _write_env_var(name: str, value: str) -> None:
    """
    Idempotent upsert of `NAME=value` in project .env. Creates the file if missing.
    Preserves other entries verbatim. Quotes values containing spaces.
    """
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# =============================================================
# /api/github/* — GitHub 同步路由
# =============================================================

@mcp.custom_route("/api/github/status", methods=["GET"])
async def api_github_status(request: Request) -> Response:
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    _gh_cfg_now = config.get("github_sync", {}) or {}
    _auto_min = int(_gh_cfg_now.get("auto_interval_minutes") or 0)
    if github_sync_instance is None:
        return JSONResponse({
            "ok": True,
            "configured": False,
            "repo": _gh_cfg_now.get("repo", ""),
            "branch": _gh_cfg_now.get("branch", "main"),
            "path_prefix": _gh_cfg_now.get("path_prefix", "ombre"),
            "token_set": bool(os.environ.get("OMBRE_GITHUB_TOKEN") or _gh_cfg_now.get("token")),
            "auto_interval_minutes": _auto_min,
        })
    return JSONResponse({"ok": True, "configured": True, "auto_interval_minutes": _auto_min, **github_sync_instance.status()})


@mcp.custom_route("/api/github/config", methods=["POST"])
async def api_github_config(request: Request) -> Response:
    global github_sync_instance
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "无效 JSON"}, status_code=400)

    token = str(body.get("token") or "").strip()
    repo = str(body.get("repo") or "").strip()
    branch = str(body.get("branch") or "main").strip() or "main"
    path_prefix = str(body.get("path_prefix") or "ombre").strip()
    auto_interval = int(body.get("auto_interval_minutes") or 0)

    if not token and not repo:
        # 清空配置
        github_sync_instance = None
        _restart_github_auto_task(0)
        gh_cfg = config.setdefault("github_sync", {})
        gh_cfg["repo"] = ""
        gh_cfg["branch"] = branch
        gh_cfg["path_prefix"] = path_prefix
        gh_cfg["auto_interval_minutes"] = 0
        return JSONResponse({"ok": True, "message": "已清空 GitHub 同步配置"})

    # 持久化到 config.yaml（含 token，config.yaml 是 bind mount 重启不丢）
    gh_cfg = config.setdefault("github_sync", {})
    if token:
        gh_cfg["token"] = token
    gh_cfg["repo"] = repo
    gh_cfg["branch"] = branch
    gh_cfg["path_prefix"] = path_prefix
    gh_cfg["auto_interval_minutes"] = auto_interval
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
    try:
        save_config: dict = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                save_config = yaml.safe_load(f) or {}
        sc_gh = save_config.setdefault("github_sync", {})
        if token:
            sc_gh["token"] = token
        sc_gh["repo"] = repo
        sc_gh["branch"] = branch
        sc_gh["path_prefix"] = path_prefix
        sc_gh["auto_interval_minutes"] = auto_interval
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(save_config, f, allow_unicode=True, default_flow_style=False)
    except Exception as e:
        logger.warning(f"[github] config.yaml 写入失败: {e}")

    # 重建实例
    _tok = token or config.get("github_sync", {}).get("token") or os.environ.get("OMBRE_GITHUB_TOKEN", "")
    github_sync_instance = GitHubSync(token=_tok, repo=repo, branch=branch, path_prefix=path_prefix)
    # 重启定时任务
    _restart_github_auto_task(auto_interval)
    return JSONResponse({"ok": True, "message": "配置已保存"})


@mcp.custom_route("/api/github/validate", methods=["POST"])
async def api_github_validate(request: Request) -> Response:
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if github_sync_instance is None:
        return JSONResponse({"ok": False, "error": "尚未配置 GitHub 同步"}, status_code=400)
    result = await github_sync_instance.validate()
    return JSONResponse(result)


@mcp.custom_route("/api/github/sync", methods=["POST"])
async def api_github_sync(request: Request) -> Response:
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if github_sync_instance is None:
        return JSONResponse({"ok": False, "error": "尚未配置 GitHub 同步，请先填写配置并保存"}, status_code=400)
    buckets_dir = config.get("buckets_dir", "")
    if not buckets_dir:
        return JSONResponse({"ok": False, "error": "buckets_dir 未配置"}, status_code=500)
    result = await github_sync_instance.sync(buckets_dir)
    return JSONResponse(result)


@mcp.custom_route("/api/host-vault", methods=["GET"])
async def api_host_vault_get(request: Request) -> Response:
    """Read the current OMBRE_HOST_VAULT_DIR (process env > project .env)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    value = _read_env_var("OMBRE_HOST_VAULT_DIR")
    return JSONResponse({
        "value": value,
        "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
        "env_file": _project_env_path(),
    })


@mcp.custom_route("/api/host-vault", methods=["POST"])
async def api_host_vault_set(request: Request) -> Response:
    """
    Persist OMBRE_HOST_VAULT_DIR to the project .env file.
    Body: {"value": "/path/to/vault"}  (empty string clears the entry)
    Note: container restart is required for docker-compose to pick up the new mount.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("value", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    value = raw.strip()

    # Reject characters that would break .env / shell parsing
    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

    try:
        _write_env_var("OMBRE_HOST_VAULT_DIR", value)
    except Exception as e:
        return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "value": value,
        "env_file": _project_env_path(),
        "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
    })


# =============================================================
# Import API — conversation history import
# 导入 API — 对话历史导入
# =============================================================

@mcp.custom_route("/api/import/upload", methods=["POST"])
async def api_import_upload(request: Request) -> Response:
    """Upload a conversation file and start import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err

    if import_engine.is_running:
        return JSONResponse({"error": "Import already running"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    filename = ""

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field or isinstance(file_field, str):
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            raw_content = raw_bytes.decode("utf-8", errors="replace")
        else:
            body = await request.body()
            raw_content = body.decode("utf-8", errors="replace")
            # Try to get filename from query params
            filename = request.query_params.get("filename", "upload")

        if not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")

    except Exception as e:
        return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

    # Start import in background
    async def _run_import():
        try:
            await import_engine.start(raw_content, filename, preserve_raw, resume)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    asyncio.create_task(_run_import())

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_content.encode()),
    })


@mcp.custom_route("/api/import/status", methods=["GET"])
async def api_import_status(request: Request) -> Response:
    """Get current import progress."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    return JSONResponse(import_engine.get_status())


@mcp.custom_route("/api/import/pause", methods=["POST"])
async def api_import_pause(request: Request) -> Response:
    """Pause the running import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if not import_engine.is_running:
        return JSONResponse({"error": "No import running"}, status_code=400)
    import_engine.pause()
    return JSONResponse({"status": "pause_requested"})


@mcp.custom_route("/api/import/patterns", methods=["GET"])
async def api_import_patterns(request: Request) -> Response:
    """Detect high-frequency patterns after import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        patterns = await import_engine.detect_patterns()
        return JSONResponse({"patterns": patterns})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/results", methods=["GET"])
async def api_import_results(request: Request) -> Response:
    """List recently imported/created buckets for review."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        limit = int(request.query_params.get("limit", "50"))
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # Sort by created time, newest first
        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        results = []
        for b in all_buckets[:limit]:
            results.append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "content": b["content"][:300],
                "type": b["metadata"].get("type", ""),
                "domain": b["metadata"].get("domain", []),
                "tags": b["metadata"].get("tags", []),
                "importance": b["metadata"].get("importance", 5),
                "created": b["metadata"].get("created", ""),
            })
        return JSONResponse({"buckets": results, "total": len(all_buckets)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/review", methods=["POST"])
async def api_import_review(request: Request) -> Response:
    """Apply review decisions: mark buckets as important/noise/pinned."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)

    applied = 0
    errors = 0
    for d in decisions:
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            continue
        try:
            if action == "important":
                await bucket_mgr.update(bid, importance=9)
            elif action == "pin":
                await bucket_mgr.update(bid, pinned=True)
            elif action == "noise":
                await bucket_mgr.update(bid, resolved=True, importance=1)
            elif action == "delete":
                await bucket_mgr.delete(bid)
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse({"applied": applied, "errors": errors})


# =============================================================
# /api/bucket/{id}/edit  — iter 1.6 §6 trace 前端
# 让 Dashboard 直接修改桶元数据：name / tags / importance / resolved /
# pinned / digested / domain。content 也支持，会同步重建 embedding。
# 内容大小受 §5 limits.max_bucket_bytes 约束；钉选量受 max_pinned 约束。
# =============================================================
@mcp.custom_route("/api/bucket/{bucket_id}/edit", methods=["PATCH", "POST"])
async def api_bucket_edit(request: Request) -> Response:
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "bucket not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updates: dict = {}

    # --- 字符串型 ---
    if isinstance(body.get("name"), str):
        nm = body["name"].strip()[:120]
        if nm:
            updates["name"] = nm

    if isinstance(body.get("tags"), list):
        # 接受 ["a","b"]
        tags = [str(t).strip() for t in body["tags"] if str(t).strip()]
        updates["tags"] = tags
    elif isinstance(body.get("tags"), str):
        # 也接受 "a, b"
        tags = [t.strip() for t in body["tags"].split(",") if t.strip()]
        updates["tags"] = tags

    if isinstance(body.get("domain"), list):
        doms = [str(d).strip() for d in body["domain"] if str(d).strip()]
        updates["domain"] = doms
    elif isinstance(body.get("domain"), str) and body["domain"].strip():
        updates["domain"] = [d.strip() for d in body["domain"].split(",") if d.strip()]

    # --- 数值/布尔型 ---
    if "importance" in body:
        try:
            imp = int(body["importance"])
            if 1 <= imp <= 10:
                updates["importance"] = imp
        except (TypeError, ValueError):
            pass

    for flag in ("resolved", "digested"):
        if flag in body:
            updates[flag] = bool(body[flag])

    # pinned 需要走配额检查
    if "pinned" in body:
        new_pinned = bool(body["pinned"])
        cur_pinned = bool(bucket["metadata"].get("pinned", False))
        if new_pinned and not cur_pinned:
            quota_err = await _check_pinned_quota()
            if quota_err:
                return JSONResponse({"error": quota_err}, status_code=400)
            updates["pinned"] = True
            updates["importance"] = 10
            updates["type"] = "permanent"
        elif (not new_pinned) and cur_pinned:
            updates["pinned"] = False
            if bucket["metadata"].get("type") == "permanent":
                updates["type"] = "dynamic"

    # content 替换 —— 走 §5 大小校验
    new_content = body.get("content")
    if isinstance(new_content, str) and new_content.strip() and new_content != bucket.get("content", ""):
        size_err = _check_content_size(new_content)
        if size_err:
            return JSONResponse({"error": size_err}, status_code=400)
        updates["content"] = new_content

    # type 字段直接改（不经 pinned 联动，调用方自己负责一致性）
    _valid_types = {"dynamic", "permanent", "feel", "plan", "letter", "i"}
    if isinstance(body.get("type"), str) and body["type"] in _valid_types:
        if body["type"] != bucket["metadata"].get("type"):
            updates["type"] = body["type"]

    if not updates:
        return JSONResponse({"error": "nothing to update"}, status_code=400)

    try:
        ok = await bucket_mgr.update(bucket_id, **updates)
        if not ok:
            return JSONResponse({"error": "update failed"}, status_code=500)
        if "content" in updates:
            try:
                await embedding_engine.generate_and_store(bucket_id, updates["content"])
            except Exception as e:
                logger.warning(f"edit: re-embedding failed for {bucket_id}: {e}")
            try:
                dehydrator.invalidate_cache(bucket["content"])
            except Exception:
                pass
        return JSONResponse({"ok": True, "id": bucket_id, "updated": list(updates.keys())})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/export  — 完整记忆打包导出
# 导出内容：所有 bucket markdown + embeddings.db + export_meta.json（含 embedding 模型信息）
# 不导出 config（避免 api_key 等密钥泄露）
# export_meta.json 中的 embedding 字段供导入端检查模型一致性。
# =============================================================
@mcp.custom_route("/api/export", methods=["GET"])
async def api_export(request: Request) -> Response:
    from starlette.responses import StreamingResponse, JSONResponse
    err = _require_auth(request)
    if err:
        return err

    import io
    import zipfile

    buckets_dir = config.get("buckets_dir", "")
    if not buckets_dir or not os.path.isdir(buckets_dir):
        return JSONResponse({"error": f"buckets_dir not found: {buckets_dir}"}, status_code=500)

    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # 1) bucket markdowns
            for root, _dirs, files in os.walk(buckets_dir):
                for fn in files:
                    if not fn.endswith(".md"):
                        continue
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, buckets_dir)
                    arc = os.path.join("buckets", rel)
                    try:
                        zf.write(full, arc)
                    except Exception as e:
                        logger.warning(f"export: skip {full}: {e}")

            # 2) embeddings.db（如果存在）
            emb_path = embedding_engine.db_path if hasattr(embedding_engine, "db_path") else None
            if emb_path and os.path.isfile(emb_path):
                try:
                    zf.write(emb_path, "embeddings.db")
                except Exception as e:
                    logger.warning(f"export: skip embeddings.db: {e}")

            # 3) export_meta.json — 包含 embedding 模型信息，供导入端检查模型一致性
            # 不包含 config（避免泄露 api_key 等密钥）
            try:
                from datetime import datetime as _dt
                _emb_backend = getattr(embedding_engine, "_backend", None)
                _emb_model = str(getattr(embedding_engine, "model", "") or "")
                try:
                    _emb_dim = int(_emb_backend.vector_dim()) if _emb_backend else 0
                except Exception:
                    _emb_dim = 0
                _emb_be_name = str(getattr(embedding_engine, "backend", "") or "")
                meta: dict = {
                    "exported_at": _dt.now().isoformat(timespec="seconds"),
                    "version": __version__,
                    "embedding": {
                        "model": _emb_model,
                        "dim": _emb_dim,
                        "backend": _emb_be_name,
                    },
                }
                # stats 失败时不影响 meta 写入（测试环境 mock 对象无法序列化）
                try:
                    meta["stats"] = await bucket_mgr.get_stats()
                except Exception:
                    pass
                zf.writestr("export_meta.json", _json_lib.dumps(meta, ensure_ascii=False, indent=2))
            except Exception as e:
                logger.warning(f"export: meta failed: {e}")
    except Exception as e:
        return JSONResponse({"error": f"export failed: {e}"}, status_code=500)

    buf.seek(0)
    fname = f"ombre_export_{int(time.time())}.zip"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# =============================================================
# /api/migrate/* — 完整记忆包（zip）导入
# 流程：POST /upload → GET /status（含冲突列表） → POST /apply（带决策）→ 轮询 GET /status
# =============================================================

@mcp.custom_route("/api/migrate/upload", methods=["POST"])
async def api_migrate_upload(request: Request) -> Response:
    """上传 ombre_export_*.zip，解析内容并识别冲突，不实际写入。

    Body: multipart/form-data，字段名 'file'；或直接 POST zip 字节（Content-Type: application/zip）。
    成功返回解析状态（含冲突列表、embedding 模型匹配情况）。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err

    if migrate_engine.is_busy:
        return JSONResponse({"error": "已有迁移任务正在进行，请等待完成后再上传"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field or isinstance(file_field, str):
                return JSONResponse({"error": "缺少 file 字段"}, status_code=400)
            zip_bytes = await file_field.read()
        else:
            zip_bytes = await request.body()

        if not zip_bytes:
            return JSONResponse({"error": "文件为空"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"读取上传内容失败: {e}"}, status_code=400)

    result = await migrate_engine.parse_zip(zip_bytes)
    if not result.get("ok"):
        return JSONResponse(result, status_code=422)
    return JSONResponse(result)


@mcp.custom_route("/api/migrate/status", methods=["GET"])
async def api_migrate_status(request: Request) -> Response:
    """查询当前迁移任务状态（解析结果、冲突列表、执行进度、重新向量化进度）。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    return JSONResponse(migrate_engine.get_status())


@mcp.custom_route("/api/migrate/apply", methods=["POST"])
async def api_migrate_apply(request: Request) -> Response:
    """执行导入，携带冲突决策。

    Body (JSON):
        decisions: {bucket_id: "skip" | "overwrite" | "keep_both"}

    无冲突的 bucket 自动导入，无需出现在 decisions 中。
    冲突但未在 decisions 中的 bucket 默认 skip（安全优先）。
    成功启动后台任务返回 202；任务完成前轮询 GET /api/migrate/status。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err

    if migrate_engine.phase != "parsed":
        return JSONResponse(
            {"error": f"当前状态为 '{migrate_engine.phase}'，apply 需要先完成 upload 解析（phase=parsed）"},
            status_code=409,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    decisions: dict[str, str] = {}
    raw_decisions = body.get("decisions", {})
    if isinstance(raw_decisions, dict):
        valid_opts = {"skip", "overwrite", "keep_both"}
        for bid, decision in raw_decisions.items():
            if isinstance(bid, str) and isinstance(decision, str) and decision in valid_opts:
                decisions[bid] = decision

    # 后台执行（apply 可能耗时较长，含重新向量化）
    async def _run_apply():
        try:
            await migrate_engine.apply(decisions)
        except Exception as e:
            logger.error(f"[migrate] background apply error: {e}", exc_info=True)

    asyncio.create_task(_run_apply())

    return JSONResponse(
        {"ok": True, "message": "导入任务已启动，请轮询 GET /api/migrate/status 查看进度"},
        status_code=202,
    )


# =============================================================
# /api/version、/api/update-info、/api/do-update、/api/author、
# /api/onboarding/status、/api/status —— 已拆分到 web/meta.py
# =============================================================


# ============================================================
# OAuth 2.0 — MCP Remote Auth —— 已拆分到 web/oauth.py（路由在其 register 内注册）。
# 这里仅把启动期 MCP 鉴权中间件要用的 _is_valid_mcp_token import 回来。
# ============================================================
from web.oauth import _is_valid_mcp_token  # noqa: F401  (used by _MCPAuthMiddleware below)


# ============================================================
# Cloudflare Tunnel 管理 —— 已拆分到 web/tunnel.py（路由在其 register 内注册）。
# 这里把启动/关停 lifespan 要用的 helper import 回来。
# ============================================================
from web.tunnel import _load_tunnel_config, _start_tunnel, _stop_tunnel  # noqa: F401


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop() -> None:
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(f"http://localhost:{OMBRE_PORT}/health", timeout=_HEALTH_PROBE_TIMEOUT_SECONDS)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive() -> None:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            # iter 2.1：合并 mcp 主实例与 mcp_extra 副实例的 streamable_http_app。
            # 两个实例各自的 streamable_http_app() 会返回独立的 starlette app，
            # 内部分别只挂 /mcp 与 /mcp-extra 一条路由 + 各自的 SessionManager lifespan。
            # 这里把副实例的 routes 与 lifespan 合并进主实例，让一个 uvicorn 进程
            # 同时承载 /mcp、/mcp-extra 与所有 dashboard custom_route。
            import contextlib as _ctxlib
            _app = mcp.streamable_http_app()
            _extra_app = mcp_extra.streamable_http_app()
            _main_lifespan = _app.router.lifespan_context
            _extra_lifespan = _extra_app.router.lifespan_context

            @_ctxlib.asynccontextmanager
            async def _combined_lifespan(app):
                async with _main_lifespan(app):
                    async with _extra_lifespan(app):
                        # Auto-start tunnel if configured
                        _tcfg = _load_tunnel_config()
                        if _tcfg.get("auto_start") and _tcfg.get("token"):
                            _ok, _msg = _start_tunnel(_tcfg["token"])
                            logger.info(f"Tunnel auto-start: {_msg}")
                        # Auto-start GitHub sync loop if configured
                        if _gh_auto_interval > 0:
                            _restart_github_auto_task(_gh_auto_interval)
                        yield
                        _stop_tunnel()

            _app.router.lifespan_context = _combined_lifespan
            _app.routes.extend(_extra_app.routes)
            logger.info(
                "MCP split / MCP 拆分：主连接器 /mcp（5 高频工具）+ 副连接器 /mcp-extra（6 低频工具）"
            )
        else:
            _app = mcp.sse_app()
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")

        # MCP Bearer token auth — pure ASGI middleware (no response buffering)
        # BaseHTTPMiddleware buffers SSE streams and breaks MCP tool listing
        import json as _json_mw

        # config.yaml: mcp_require_auth: false → 完全跳过 OAuth 检查，
        # 任何客户端（GPT / GLM / 自定义前端）可免认证直连 /mcp。
        # 不填或 true → 保持默认：必须 OAuth Bearer token。
        _mcp_auth_required = bool(config.get("mcp_require_auth", True))

        class _MCPAuthMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and _mcp_auth_required:
                    path = scope.get("path", "")
                    if path.startswith("/mcp"):
                        headers = {k.lower(): v for k, v in scope.get("headers", [])}
                        auth = headers.get(b"authorization", b"").decode("latin-1")
                        if not (auth.startswith("Bearer ") and _is_valid_mcp_token(auth[7:])):
                            # Build public base URL from ASGI scope headers
                            proto = headers.get(b"x-forwarded-proto", b"").decode() or scope.get("scheme", "http")
                            host = (headers.get(b"x-forwarded-host") or headers.get(b"host", b"")).decode()
                            base = f"{proto}://{host}"
                            ww_auth = (
                                f'Bearer realm="Ombre Brain",'
                                f' resource_metadata="{base}/.well-known/oauth-protected-resource"'
                            )
                            body = _json_mw.dumps({
                                "error": "Unauthorized",
                                "resource_metadata": f"{base}/.well-known/oauth-protected-resource",
                            }).encode()
                            await send({"type": "http.response.start", "status": 401, "headers": [
                                [b"content-type", b"application/json"],
                                [b"www-authenticate", ww_auth.encode()],
                                [b"content-length", str(len(body)).encode()],
                            ]})
                            await send({"type": "http.response.body", "body": body, "more_body": False})
                            return
                await self.app(scope, receive, send)

        _app.add_middleware(_MCPAuthMiddleware)
        if _mcp_auth_required:
            logger.info("MCP OAuth middleware enabled / MCP OAuth 中间件已启用")
        else:
            logger.info("MCP auth disabled (mcp_require_auth: false) — open access / MCP 认证已关闭，所有客户端可直连")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        # stdio / sse：单连接器无 5 工具上限，把 mcp_extra 的工具回灌到 mcp
        # 让所有 11 个工具仍在同一连接器里暴露（兼容旧 Claude Desktop 配置）。
        # 依赖 FastMCP._tool_manager 私有结构；若未来版本变化，回退为只暴露主集 5 工具。
        try:
            mcp._tool_manager._tools.update(mcp_extra._tool_manager._tools)
            logger.info(
                f"stdio/sse 单连接器模式：已回灌 {len(mcp_extra._tool_manager._tools)} 个副集工具"
            )
        except AttributeError as e:
            logger.warning(
                f"FastMCP 内部结构变化，stdio 模式仅暴露主集 5 工具：{e}"
            )
        mcp.run(transport=transport)
