"""api.yaml 文件路径和写入工具。"""


# 迁移说明：
# 修改原因：该模块承载业务逻辑，不应继续放在 utils_pkg 这种通用工具包中。
# 修改方式：按照 Scout 的归位方案迁移到 core 对应业务模块，并只调整必要的内部导入路径。
# 目的：让业务代码按领域归属维护，同时保留根 utils.py 和 utils_pkg shim 的旧导入兼容性。
import os

from core.log_config import logger

from .codec import _quote_colon_strings, yaml
from .serialization import _sanitize_config_for_persistence


# 修改原因：实现文件迁移到 core/config 后，__file__ 会指向包目录，默认 api.yaml 路径会错误地落到 core/api.yaml。
# 修改方式：把基础目录从 core/config 提升两级到项目根目录，再按旧规则拼接 api.yaml。
# 目的：保持拆分前默认读取和写入项目根目录 api.yaml 的行为。
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_YAML_PATH = os.path.abspath(os.getenv("API_YAML_PATH") or os.path.join(_BASE_DIR, "api.yaml"))


def save_api_yaml(config_data):
    """将配置持久化到 api.yaml。

    写入策略：
    1. 优先原子写入（临时文件 + os.replace），避免写入中断导致文件损坏。
    2. 若 os.replace 失败（常见于 Docker 单文件挂载，挂载点不可被 rename 替换，
       报 Errno 16 Device or resource busy），自动回退为直接写入目标文件。
    3. 若目标路径是目录，则直接报错，并提示用户修正挂载方式。
    """

    import errno
    import tempfile

    # 修改原因：配置持久化清理逻辑已经拆到 config_persistence，文件写入模块不应复制一份清理规则。
    # 修改方式：统一调用 _sanitize_config_for_persistence，再保留本函数原有的安全检查和原子写入。
    # 目的：让 JSON、YAML 导出和 api.yaml 写入使用同一套运行时字段过滤规则。
    processed_data = _sanitize_config_for_persistence(config_data)
    processed_data = _quote_colon_strings(processed_data)

    target_path = os.path.abspath(API_YAML_PATH)

    # 修改原因：手动编辑 api.yaml 或配置解析失败时，规范化后的 providers 可能变成空列表。
    # 修改方式：在实际写文件前读取旧文件，如果旧文件有 providers 而新配置为 0 个 providers，则拒绝本次写入。
    # 目的：避免启动或同步配置时把原本正确的渠道配置覆盖成空配置。
    if os.path.exists(target_path):
        try:
            with open(target_path, 'r', encoding='utf-8') as f:
                existing_data = yaml.load(f)
            existing_providers = existing_data.get('providers', []) if isinstance(existing_data, dict) else []
            new_providers = processed_data.get('providers', [])
            if len(existing_providers) > 0 and len(new_providers) == 0:
                logger.error(
                    f"[save_api_yaml] BLOCKED: refusing to overwrite {len(existing_providers)} providers with empty list. "
                    f"This usually indicates a config parsing error. Original file preserved."
                )
                return
            import shutil
            import time as _time
            if len(existing_providers) > 5 and len(new_providers) < len(existing_providers) * 0.5 and not config_data.get("_allow_mass_shrink"):
                try:
                    _snap = f"{target_path}.bak_autosave_{_time.strftime('%Y%m%d_%H%M%S')}"
                    shutil.copy2(target_path, _snap)
                    logger.warning(f"[save_api_yaml] old config snapshotted to {_snap}")
                except Exception as _snap_err:
                    logger.warning(f"[save_api_yaml] snapshot before overwrite failed: {_snap_err}")
                logger.error(
                    f"[save_api_yaml] BLOCKED: refusing to shrink providers from {len(existing_providers)} to {len(new_providers)} without _allow_mass_shrink."
                )
                raise RuntimeError(
                    f"Blocked unsafe api.yaml overwrite: providers dropped from {len(existing_providers)} to {len(new_providers)}"
                )
            # 每次正常保存前在 data/config_backups/ 保留最近 10 份滚动备份
            if len(existing_providers) >= 10:
                try:
                    _bak_dir = os.path.join(os.path.dirname(target_path) or ".", "data", "config_backups")
                    os.makedirs(_bak_dir, exist_ok=True)
                    _rolling_snap = os.path.join(_bak_dir, f"api.yaml.bak_{_time.strftime('%Y%m%d_%H%M%S')}")
                    shutil.copy2(target_path, _rolling_snap)
                    _all_baks = sorted(
                        [os.path.join(_bak_dir, fn) for fn in os.listdir(_bak_dir) if fn.startswith("api.yaml.bak_")],
                        key=lambda p: os.path.getmtime(p),
                    )
                    for _old_b in _all_baks[:-10]:
                        try:
                            os.remove(_old_b)
                        except OSError:
                            pass
                except Exception as _rb_err:
                    logger.warning(f"[save_api_yaml] rolling backup warning: {_rb_err}")
        except Exception as e:
            logger.warning(f"[save_api_yaml] Could not read existing file for safety check: {e}")

    target_dir = os.path.dirname(target_path) or "."
    os.makedirs(target_dir, exist_ok=True)

    if os.path.isdir(target_path):
        raise RuntimeError(
            f"Configured api.yaml path '{target_path}' is a directory, not a file. "
            f"This usually happens when Docker bind-mounts a missing host path as a directory. "
            f"For Docker, prefer CONFIG_STORAGE=db with a persistent /home/data volume, "
            f"or mount a directory and set API_YAML_PATH to a file inside it."
        )

    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".api.yaml.", suffix=".tmp", dir=target_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(processed_data, f)
            f.flush()
            os.fsync(f.fileno())

        try:
            os.replace(temp_path, target_path)
            temp_path = None
            return
        except OSError as e:
            replace_errno = getattr(e, "errno", None)
            if replace_errno not in {errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES}:
                raise

            logger.warning(
                f"Atomic replace unavailable for '{target_path}', falling back to direct write. "
                f"This is common with Docker single-file bind mounts. err={e}"
            )

            try:
                os.unlink(temp_path)
            except OSError:
                pass
            temp_path = None

        with open(target_path, "w", encoding="utf-8") as f:
            yaml.dump(processed_data, f)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise RuntimeError(f"Failed to save api.yaml to '{target_path}': {e}") from e
