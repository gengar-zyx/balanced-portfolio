"""Long-running Feishu WebSocket command receiver."""

from __future__ import annotations

import json
import logging
import os

from . import db
from .feishu_commands import IncomingCommand, ingest_command, load_command_config
from .notifications import load_feishu_config
from .settings import load_settings

logger = logging.getLogger(__name__)


def _handle_message(data) -> None:
    try:
        event = data.event
        message = event.message
        sender = event.sender
        content = json.loads(message.content or "{}")
        text = content.get("text", "") if isinstance(content, dict) else ""
        mentions = tuple(
            mention.key for mention in (message.mentions or []) if getattr(mention, "key", None)
        )
        event_id = getattr(getattr(data, "header", None), "event_id", None) or message.message_id
        ingest_command(IncomingCommand(
            event_id=event_id,
            message_id=message.message_id,
            chat_id=message.chat_id,
            chat_type=message.chat_type or "",
            sender_type=sender.sender_type or "",
            message_type=message.message_type or "",
            text=text,
            mention_keys=mentions,
        ))
    except Exception:  # noqa: BLE001 - callback errors must not terminate the WebSocket process
        logger.exception("处理飞书消息事件失败")


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.getenv("BP_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    command_config = load_command_config()
    feishu = load_feishu_config()
    if not command_config.enabled:
        logger.info("飞书命令未启用，feishu-bot 退出")
        return 0

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: F401 - handler type registration

    settings = load_settings()
    db.init_pool(settings)
    handler = (
        lark.EventDispatcherHandler.builder("", "", lark.LogLevel.WARNING)
        .register_p2_im_message_receive_v1(_handle_message)
        .build()
    )
    client = lark.ws.Client(
        feishu.app_id,
        feishu.app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.WARNING,
    )
    logger.info("飞书 /position 长连接服务启动")
    client.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
