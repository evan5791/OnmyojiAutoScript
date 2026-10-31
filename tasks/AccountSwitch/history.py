"""Small local audit files using the original OAS logging directory."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import threading

from module.logger import logger

_lock = threading.Lock()


def record_event(config, account, platform, stage, details=None, *, day=None, occurred_at=None):
    stamp = occurred_at or datetime.now(timezone(timedelta(hours=8)))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone(timedelta(hours=8)))
    event = {'occurred_at': stamp.isoformat(timespec='seconds'),
             'day': day or (stamp - timedelta(hours=6)).date().isoformat(),
             'account': account, 'platform': platform, 'stage': stage, 'details': details or {}}
    logger.info('小号记录 [%s/%s] %s %s', platform, account or '本轮', stage,
                json.dumps(details or {}, ensure_ascii=False, default=str))
    # Unit-test/config proxies have no persisted profile. The production OAS
    # model supplies config_name; never invent an executable configuration.
    model = getattr(config, 'model', None)
    name = getattr(model, 'config_name', None)
    if not isinstance(name, str) or not name:
        return event
    safe_name = re.sub(r'[^\w.-]', '_', name)
    folder = Path('log') / 'account-switch' / safe_name
    try:
        with _lock:
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / (event['day'] + '.jsonl')).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
    except OSError as exc:
        logger.warning('小号历史写入失败，已确认进度仍保留：%s', exc)
    return event
