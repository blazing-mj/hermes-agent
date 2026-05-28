"""Phase 7 Telegram team-os approval rail tests."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.platforms.telegram import TelegramAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_delivery(task_id="AGENTS-200", approval_id=42):
    from hermes_cli.team_os.approvals import build_approval_sample
    from hermes_cli.team_os.delivery import TelegramApprovalDelivery

    sample = build_approval_sample(
        task_id=task_id,
        title="Approval delivery test",
        action="run database migration",
    )
    return TelegramApprovalDelivery.from_approval_sample(
        sample, approval_id=approval_id, dry_run=True,
    )


class TestSendTeamOSApproval:
    @pytest.mark.asyncio
    async def test_send_team_os_approval_sends_message(self, tmp_path):
        from gateway.platforms.telegram import InlineKeyboardButton as _IKB
        _IKB.reset_mock()

        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 17
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        delivery = _make_delivery(task_id="AGENTS-200", approval_id=42)
        result = await adapter.send_team_os_approval(
            chat_id="12345",
            delivery=delivery,
            db_path=str(tmp_path / "team-os.db"),
        )

        assert result.success is True
        assert result.message_id == "17"
        adapter._bot.send_message.assert_called_once()
        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert "AGENTS-200" in kwargs["text"]
        assert kwargs["reply_markup"] is not None

        # state stored under a token (not approval_id directly)
        assert len(adapter._team_os_approval_state) == 1
        token = list(adapter._team_os_approval_state.keys())[0]
        stored_approval_id, stored_db_path = adapter._team_os_approval_state[token]
        assert stored_approval_id == 42
        assert stored_db_path == str(tmp_path / "team-os.db")

        # 4 InlineKeyboardButton calls — one per action — all sharing the token
        button_calls = list(_IKB.call_args_list)
        assert kwargs["reply_markup"] is not None
        actions_seen = sorted({
            call.kwargs.get("callback_data", "").split(":")[1]
            for call in button_calls
            if call.kwargs.get("callback_data", "").endswith(f":{token}")
        })
        assert actions_seen == ["approve", "defer", "modify", "reject"]

    @pytest.mark.asyncio
    async def test_send_team_os_approval_not_connected(self, tmp_path):
        adapter = _make_adapter()
        adapter._bot = None

        delivery = _make_delivery()
        result = await adapter.send_team_os_approval(
            chat_id="12345",
            delivery=delivery,
            db_path=str(tmp_path / "team-os.db"),
        )
        assert result.success is False


class TestTeamOSApprovalCallbacks:
    @pytest.mark.asyncio
    async def test_ta_approve_callback_records_decision(self, tmp_path):
        from hermes_cli.team_os.approvals import ReversibilityCategory
        from hermes_cli.team_os.db import TeamOSState

        adapter = _make_adapter()
        db_path = tmp_path / "team-os.db"
        db = TeamOSState(db_path)
        approval_id = db.create_approval_request(
            task_id="AGENTS-300",
            title="approve via button",
            action="run migration",
            reversibility_category=ReversibilityCategory.DATA_MIGRATION,
            reversibility_reason="database migration requires explicit approval",
            prompt="prompt text",
        )

        token = "abcdef0123456789"
        adapter._team_os_approval_state[token] = (approval_id, str(db_path))

        query = AsyncMock()
        query.data = f"ta:approve:{token}"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = "12345"
        query.from_user.first_name = "Norbert"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        record = db.get_approval_request(approval_id)
        assert record["status"] == "approved"
        assert record["decision"] == "approve"
        assert record["actor"] == "Norbert"

        assert token not in adapter._team_os_approval_state
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_ta_reject_callback_enters_text_capture(self, tmp_path):
        adapter = _make_adapter()
        token = "rejecttoken00000"
        adapter._team_os_approval_state[token] = (5, str(tmp_path / "team-os.db"))

        query = AsyncMock()
        query.data = f"ta:reject:{token}"
        query.message = MagicMock()
        query.message.chat_id = 67890
        query.from_user = MagicMock()
        query.from_user.id = "67890"
        query.from_user.first_name = "Alice"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        # state is moved into modify_capture; original token still in approval_state
        # so the text-capture path can retrieve approval_id + db_path
        assert "67890:67890" in adapter._team_os_modify_capture
        captured_token, action = adapter._team_os_modify_capture["67890:67890"]
        assert captured_token == token
        assert action == "reject"
        assert token in adapter._team_os_approval_state
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_ta_defer_callback_enters_text_capture(self, tmp_path):
        adapter = _make_adapter()
        token = "defertoken000000"
        adapter._team_os_approval_state[token] = (6, str(tmp_path / "team-os.db"))

        query = AsyncMock()
        query.data = f"ta:defer:{token}"
        query.message = MagicMock()
        query.message.chat_id = 11111
        query.from_user = MagicMock()
        query.from_user.id = "11111"
        query.from_user.first_name = "Bea"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        assert "11111:11111" in adapter._team_os_modify_capture
        captured_token, action = adapter._team_os_modify_capture["11111:11111"]
        assert captured_token == token
        assert action == "defer"

    @pytest.mark.asyncio
    async def test_ta_modify_callback_enters_text_capture(self, tmp_path):
        adapter = _make_adapter()
        token = "modifytoken00000"
        adapter._team_os_approval_state[token] = (7, str(tmp_path / "team-os.db"))

        query = AsyncMock()
        query.data = f"ta:modify:{token}"
        query.message = MagicMock()
        query.message.chat_id = 22222
        query.from_user = MagicMock()
        query.from_user.id = "22222"
        query.from_user.first_name = "Cory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        assert "22222:22222" in adapter._team_os_modify_capture
        captured_token, action = adapter._team_os_modify_capture["22222:22222"]
        assert captured_token == token
        assert action == "modify"

    @pytest.mark.asyncio
    async def test_ta_callback_unauthorized_user_blocked(self, tmp_path):
        from hermes_cli.team_os.approvals import ReversibilityCategory
        from hermes_cli.team_os.db import TeamOSState

        adapter = _make_adapter()
        db_path = tmp_path / "team-os.db"
        db = TeamOSState(db_path)
        approval_id = db.create_approval_request(
            task_id="AGENTS-400",
            title="unauthorized attempt",
            action="run migration",
            reversibility_category=ReversibilityCategory.DATA_MIGRATION,
            reversibility_reason="db migration",
            prompt="prompt",
        )

        token = "unauthtoken00000"
        adapter._team_os_approval_state[token] = (approval_id, str(db_path))

        query = AsyncMock()
        query.data = f"ta:approve:{token}"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = "999"
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False):
            await adapter._handle_callback_query(update, context)

        record = db.get_approval_request(approval_id)
        assert record["status"] == "pending"
        assert record["decision"] is None
        assert token in adapter._team_os_approval_state
        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_ta_already_resolved(self):
        adapter = _make_adapter()
        # No token in state — already resolved

        query = AsyncMock()
        query.data = "ta:approve:doesnotexist0000"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = "12345"
        query.from_user.first_name = "Norbert"
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "already" in query.answer.call_args[1]["text"].lower()


class TestTeamOSTextCapture:
    @pytest.mark.asyncio
    async def test_text_capture_reject_records_decision(self, tmp_path):
        from hermes_cli.team_os.approvals import ReversibilityCategory
        from hermes_cli.team_os.db import TeamOSState

        adapter = _make_adapter()
        db_path = tmp_path / "team-os.db"
        db = TeamOSState(db_path)
        approval_id = db.create_approval_request(
            task_id="AGENTS-501",
            title="text-capture reject",
            action="run migration",
            reversibility_category=ReversibilityCategory.DATA_MIGRATION,
            reversibility_reason="db migration",
            prompt="prompt",
        )

        token = "rejecttextcap000"
        chat_id = 67890
        user_id = 111
        adapter._team_os_approval_state[token] = (approval_id, str(db_path))
        adapter._team_os_modify_capture[f"{chat_id}:{user_id}"] = (token, "reject")

        msg = MagicMock()
        msg.chat_id = chat_id
        msg.chat = MagicMock(type="private")
        msg.message_thread_id = None
        msg.from_user = MagicMock()
        msg.from_user.id = user_id
        msg.from_user.first_name = "Norbert"
        msg.text = "too risky"

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            consumed = await adapter._maybe_capture_team_os_reply(msg)

        assert consumed is True
        record = db.get_approval_request(approval_id)
        assert record["decision"] == "reject"
        assert record["decision_reason"] == "too risky"
        assert record["actor"] == "Norbert"
        assert adapter._team_os_modify_capture == {}
        assert token not in adapter._team_os_approval_state

    @pytest.mark.asyncio
    async def test_text_capture_approve_modified_sets_db_status_approved(self, tmp_path):
        from hermes_cli.team_os.approvals import ApprovalStatus, ReversibilityCategory
        from hermes_cli.team_os.db import TeamOSState

        adapter = _make_adapter()
        db_path = tmp_path / "team-os.db"
        db = TeamOSState(db_path)
        approval_id = db.create_approval_request(
            task_id="AGENTS-502",
            title="text-capture approve-modified",
            action="run migration",
            reversibility_category=ReversibilityCategory.DATA_MIGRATION,
            reversibility_reason="db migration",
            prompt="prompt",
        )

        token = "modifytextcap000"
        chat_id = 11111
        user_id = 222
        adapter._team_os_approval_state[token] = (approval_id, str(db_path))
        adapter._team_os_modify_capture[f"{chat_id}:{user_id}"] = (token, "modify")

        msg = MagicMock()
        msg.chat_id = chat_id
        msg.chat = MagicMock(type="private")
        msg.message_thread_id = None
        msg.from_user = MagicMock()
        msg.from_user.id = user_id
        msg.from_user.first_name = "Cory"
        msg.text = "reduced scope only"

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            consumed = await adapter._maybe_capture_team_os_reply(msg)

        assert consumed is True
        record = db.get_approval_request(approval_id)
        assert record["decision"] == "approve-modified"
        assert record["modified_scope"] == "reduced scope only"
        # decision_to_status("approve-modified") -> ApprovalStatus.APPROVED
        assert record["status"] == ApprovalStatus.APPROVED.value
        assert adapter._team_os_modify_capture == {}
        assert token not in adapter._team_os_approval_state

    @pytest.mark.asyncio
    async def test_text_capture_wrong_user_ignored(self, tmp_path):
        from hermes_cli.team_os.db import TeamOSState

        adapter = _make_adapter()
        db_path = tmp_path / "team-os.db"

        token = "wronguserrtoken0"
        chat_id = 12345
        owner_user_id = 111
        adapter._team_os_approval_state[token] = (99, str(db_path))
        adapter._team_os_modify_capture[f"{chat_id}:{owner_user_id}"] = (token, "reject")

        msg = MagicMock()
        msg.chat_id = chat_id
        msg.chat = MagicMock(type="group")
        msg.message_thread_id = None
        msg.from_user = MagicMock()
        msg.from_user.id = 222
        msg.from_user.first_name = "Mallory"
        msg.text = "i wasn't asked, but here you go"

        with patch.object(
            TeamOSState, "record_approval_decision"
        ) as record_mock, patch.dict(
            os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False
        ):
            consumed = await adapter._maybe_capture_team_os_reply(msg)

        assert consumed is False
        record_mock.assert_not_called()
        # owner's capture must still be parked for the real approver
        assert adapter._team_os_modify_capture == {
            f"{chat_id}:{owner_user_id}": (token, "reject"),
        }
        assert token in adapter._team_os_approval_state
