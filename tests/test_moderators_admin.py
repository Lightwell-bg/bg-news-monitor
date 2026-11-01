"""Moderators panel: list, add flow, delete, report recipients, /start."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest
from aiogram.types import MessageOriginHiddenUser, MessageOriginUser, ReplyKeyboardRemove, User

from news_monitor.config.runtime_store import RuntimeSettingsStore
from news_monitor.telegram.access import MAX_MODERATORS, AccessControl
from news_monitor.telegram.admin_ui import PendingInputStore
from news_monitor.telegram.moderators_admin import (
    ACTION_ADD,
    ACTION_CANCEL,
    ACTION_DELETE,
    ACTION_DELETE_CONFIRM,
    ACTION_OPEN,
    ACTION_REPORT,
    ACTION_TODAY,
    ACTION_TOGGLE,
    INPUT_ADD_MODERATOR,
    USER_REQUEST_ID,
    IncomingInput,
    ModeratorsAdminService,
    ModeratorView,
    build_callback_data,
    handle_moderators_callback,
    handle_moderators_input,
    handle_start,
    incoming_from_message,
    render_moderators,
    report_keyboard,
)
from news_monitor.telegram.settings_admin import SettingsAdminService
from tests.conftest import ADMIN_ID, SECOND_ADMIN_ID, STRANGER_ID, FakeBot
from tests.test_bot_handlers import FakeCallbackQuery, FakeUser

MODERATOR_ID = 700100200
NEW_ID = 700100300


class Chat:
    def __init__(self, full_name: str = "", title: str = "") -> None:
        self.full_name = full_name
        self.title = title


class Me:
    username = "news_bot"


class PanelBot(FakeBot):
    """FakeBot with get_chat, get_me and a send failure switch."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_send: set[int] = set()
        self.chat_calls = 0
        self.names = {ADMIN_ID: "Владелец Иван", SECOND_ADMIN_ID: "Мария"}
        self.has_username = True

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_send:
            raise RuntimeError("chat not found")
        return await super().send_message(chat_id, text, **kwargs)

    async def get_chat(self, chat_id):
        self.chat_calls += 1
        if chat_id not in self.names:
            raise RuntimeError("chat not found")
        return Chat(full_name=self.names[chat_id])

    async def get_me(self):
        if not self.has_username:
            raise RuntimeError("no network")
        return Me()


@dataclass
class FakeAnswerMessage:
    answers: list
    text: str = ""
    users_shared: object = None
    forward_origin: object = None

    def __init__(self, **kwargs) -> None:
        self.answers = []
        self.text = kwargs.get("text", "")
        self.users_shared = kwargs.get("users_shared")
        self.forward_origin = kwargs.get("forward_origin")
        self.from_user = kwargs.get("from_user")

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


@pytest.fixture
def bot() -> PanelBot:
    return PanelBot()


@pytest.fixture
def access(settings, repository) -> AccessControl:
    return AccessControl(settings, repository)


@pytest.fixture
def store(tmp_path) -> RuntimeSettingsStore:
    return RuntimeSettingsStore(tmp_path / "runtime.yaml")


@pytest.fixture
def pending() -> PendingInputStore:
    return PendingInputStore()


@pytest.fixture
def service(settings, repository, access, store, bot, pending) -> ModeratorsAdminService:
    return ModeratorsAdminService(
        access=access,
        store=store,
        repository=repository,
        bot=bot,
        settings=settings,
        pending=pending,
        today=lambda: date(2026, 10, 21),
    )


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def _open_add(service, user_id=ADMIN_ID):
    return await service.execute_callback(
        data=build_callback_data(ACTION_ADD), user_id=user_id
    )


# ------------------------------------------------------------------ render


def test_render_lists_names_escaped_and_count():
    views = [
        ModeratorView(1, "<script>alert(1)</script>"),
        ModeratorView(2, ""),
        ModeratorView(3, "x" * 200),
    ]
    text = render_moderators(views)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "2. без имени (ID 2)" in text
    assert "x" * 65 not in text
    assert f"3 из {MAX_MODERATORS}" in text


def test_render_empty_list():
    assert "Модераторов пока нет." in render_moderators([])


def test_callbacks_fit_the_telegram_limit():
    views = [ModeratorView(10**13 - i, "Имя") for i in range(MAX_MODERATORS)]
    from news_monitor.telegram.moderators_admin import moderators_keyboard

    for data in _callbacks(moderators_keyboard(views)):
        assert len(data.encode("utf-8")) <= 64
    rows = [(10**13 - i, f"A{i}", bool(i % 2)) for i in range(5)]
    for data in _callbacks(report_keyboard(rows)):
        assert len(data.encode("utf-8")) <= 64


async def test_main_screen_buttons(service, access):
    await access.add_moderator(MODERATOR_ID, "Пётр <b>", ADMIN_ID)
    reply = await service.execute_command(user_id=ADMIN_ID)
    texts = _texts(reply.keyboard)
    assert texts[0] == "➕ Добавить"
    assert "🗑 Пётр <b>" in texts
    assert "📊 Отчёт" in texts
    assert texts[-1] == "⬅ К настройкам"
    assert "Пётр &lt;b&gt;" in reply.text


def test_settings_keyboard_has_moderators_button(settings, store):
    service = SettingsAdminService(settings=settings, store=store)
    reply = service.show_settings()
    assert "mod:open" in _callbacks(reply.keyboard)
    assert "👥 Модераторы" in _texts(reply.keyboard)


# ------------------------------------------------------------------ denied


async def test_non_admin_denied_command_and_callback(service, access):
    await access.add_moderator(MODERATOR_ID, "", ADMIN_ID)
    for user in (STRANGER_ID, MODERATOR_ID):
        reply = await service.execute_command(user_id=user)
        assert "Недостаточно прав" in reply.text
        cb = await service.execute_callback(
            data=build_callback_data(ACTION_ADD), user_id=user
        )
        assert cb.alert and cb.text == ""
        assert not service.has_pending_input(user)
    assert access.moderator_ids == (MODERATOR_ID,)


# ------------------------------------------------------------------- add


async def test_add_press_opens_slot_and_picker_message(service, pending):
    reply = await _open_add(service)
    assert pending.get(ADMIN_ID).kind == INPUT_ADD_MODERATOR
    assert "Telegram ID" in reply.text
    assert _callbacks(reply.keyboard) == ["mod:cxl"]


async def test_add_via_users_shared(service, access, bot):
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(
            shared_request_id=USER_REQUEST_ID, shared_users=((NEW_ID, "Анна Иванова"),)
        ),
        user_id=ADMIN_ID,
    )
    assert outcome.done
    assert access.is_moderator(NEW_ID)
    assert "Анна Иванова назначен" in outcome.reply.text
    assert "Ему отправлено уведомление" in outcome.reply.text
    assert not service.has_pending_input(ADMIN_ID)
    assert "Анна Иванова" in outcome.followup.text
    sent = bot.messages_to(NEW_ID)
    assert len(sent) == 1 and "модератором" in sent[0].text and "Одобрить" in sent[0].text


async def test_users_shared_with_wrong_request_id_keeps_waiting(service, access):
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(shared_request_id=1, shared_users=((NEW_ID, "A"),)),
        user_id=ADMIN_ID,
    )
    assert not outcome.done
    assert not access.is_moderator(NEW_ID)
    assert service.has_pending_input(ADMIN_ID)


async def test_add_via_typed_id(service, access):
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(text=f" {NEW_ID} "), user_id=ADMIN_ID
    )
    assert outcome.done and access.is_moderator(NEW_ID)
    assert f"ID {NEW_ID} назначен" in outcome.reply.text


@pytest.mark.parametrize("text", ["0", "-5", "12345678901234567", "привет", "/sources", "abc123", "٣"])
async def test_bad_text_keeps_waiting(service, access, text):
    await _open_add(service)
    outcome = await service.execute_input(incoming=IncomingInput(text=text), user_id=ADMIN_ID)
    assert not outcome.done
    assert outcome.reply.keyboard is not None
    assert service.has_pending_input(ADMIN_ID)
    assert access.moderator_ids == ()


async def test_garbage_without_text_keeps_waiting(service, access):
    await _open_add(service)
    outcome = await service.execute_input(incoming=IncomingInput(), user_id=ADMIN_ID)
    assert not outcome.done and service.has_pending_input(ADMIN_ID)


async def test_add_via_forward(service, access):
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(forward_user=(NEW_ID, "Олег", False)), user_id=ADMIN_ID
    )
    assert outcome.done and access.is_moderator(NEW_ID)


async def test_forwarded_bot_rejected(service, access):
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(forward_user=(NEW_ID, "Бот", True)), user_id=ADMIN_ID
    )
    assert not outcome.done and not access.is_moderator(NEW_ID)


async def test_hidden_forward_explains(service, access):
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(forward_hidden=True), user_id=ADMIN_ID
    )
    assert not outcome.done
    assert "/start" in outcome.reply.text and "скрыл" in outcome.reply.text
    assert service.has_pending_input(ADMIN_ID)


async def test_cancel_clears_slot(service, pending):
    await _open_add(service)
    reply = await service.execute_callback(
        data=build_callback_data(ACTION_CANCEL), user_id=ADMIN_ID
    )
    assert pending.get(ADMIN_ID) is None
    assert reply.notice == "Ввод отменён."
    assert "Модераторов пока нет." in reply.text


async def test_other_admin_cannot_complete_slot(service, access):
    await _open_add(service, ADMIN_ID)
    outcome = await service.execute_input(
        incoming=IncomingInput(text=str(NEW_ID)), user_id=SECOND_ADMIN_ID
    )
    assert not outcome.reply.text and not outcome.done
    assert access.moderator_ids == ()
    assert service.has_pending_input(ADMIN_ID)


async def test_input_from_non_admin_denied(service, access):
    outcome = await service.execute_input(
        incoming=IncomingInput(text=str(NEW_ID)), user_id=STRANGER_ID
    )
    assert "Недостаточно прав" in outcome.reply.text
    assert access.moderator_ids == ()


async def test_adding_admin_is_refused(service, access):
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(text=str(SECOND_ADMIN_ID)), user_id=ADMIN_ID
    )
    assert outcome.done
    assert "Это администратор — у него и так все права." in outcome.reply.text
    assert access.moderator_ids == ()


async def test_already_moderator(service, access):
    await access.add_moderator(NEW_ID, "", ADMIN_ID)
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(text=str(NEW_ID)), user_id=ADMIN_ID
    )
    assert "уже модератор" in outcome.reply.text


async def test_limit_reached(service, access):
    for i in range(MAX_MODERATORS):
        await access.add_moderator(5000 + i, "", ADMIN_ID)
    reply = await _open_add(service)
    assert "предел" in reply.notice or "предел" in reply.text
    assert not service.has_pending_input(ADMIN_ID)
    # Race: a slot opened earlier, the limit is hit before the answer.
    service.pending_inputs.start(ADMIN_ID, INPUT_ADD_MODERATOR)
    outcome = await service.execute_input(
        incoming=IncomingInput(text=str(NEW_ID)), user_id=ADMIN_ID
    )
    assert "предел" in outcome.reply.text and not access.is_moderator(NEW_ID)


async def test_welcome_failure_hint_with_username(service, access, bot):
    bot.fail_send.add(NEW_ID)
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(text=str(NEW_ID)), user_id=ADMIN_ID
    )
    assert access.is_moderator(NEW_ID)
    assert "Человек ещё не запускал бота" in outcome.reply.text
    assert "@news_bot" in outcome.reply.text
    assert "«Запустить» (Start)" in outcome.reply.text


async def test_welcome_failure_hint_without_username(service, access, bot):
    bot.fail_send.add(NEW_ID)
    bot.has_username = False
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(text=str(NEW_ID)), user_id=ADMIN_ID
    )
    assert "Человек ещё не запускал бота" in outcome.reply.text
    assert "@" not in outcome.reply.text


async def test_script_name_is_escaped_and_cut(service, access):
    await _open_add(service)
    outcome = await service.execute_input(
        incoming=IncomingInput(shared_request_id=USER_REQUEST_ID, shared_users=((NEW_ID, "<script>" + "z" * 100),)),
        user_id=ADMIN_ID,
    )
    assert "<script>" not in outcome.reply.text
    assert "<script>" not in outcome.followup.text
    stored = (await access._repository.list_moderators())[0].display_name
    assert len(stored) <= 64


# ---------------------------------------------------------------- message


def test_incoming_from_message_variants():
    sender = User(id=NEW_ID, is_bot=False, first_name="Ольга", last_name="К")
    origin = MessageOriginUser(type="user", date=0, sender_user=sender)
    incoming = incoming_from_message(FakeAnswerMessage(forward_origin=origin))
    assert incoming.forward_user == (NEW_ID, "Ольга К", False)
    hidden = MessageOriginHiddenUser(type="hidden_user", date=0, sender_user_name="X")
    assert incoming_from_message(FakeAnswerMessage(forward_origin=hidden)).forward_hidden

    class Shared:
        request_id = USER_REQUEST_ID

        class _U:
            user_id = NEW_ID
            first_name = "Ян"
            last_name = None

        users = [_U()]

    shared = incoming_from_message(FakeAnswerMessage(users_shared=Shared()))
    assert shared.shared_users == ((NEW_ID, "Ян"),)
    assert shared.shared_request_id == USER_REQUEST_ID


async def test_handler_removes_reply_keyboard_after_add(service, access):
    await _open_add(service)
    message = FakeAnswerMessage(text=str(NEW_ID), from_user=FakeUser(ADMIN_ID))
    await handle_moderators_input(message, service)
    assert len(message.answers) == 2
    assert isinstance(message.answers[0][1]["reply_markup"], ReplyKeyboardRemove)
    assert access.is_moderator(NEW_ID)


async def test_callback_handler_sends_picker_keyboard(service):
    callback = FakeCallbackQuery(
        data=build_callback_data(ACTION_ADD), from_user=FakeUser(ADMIN_ID)
    )
    sent = []

    async def answer(text, **kwargs):
        sent.append((text, kwargs))

    callback.message.answer = answer
    await handle_moderators_callback(callback, service)
    markup = sent[0][1]["reply_markup"]
    key = markup.keyboard[0][0]
    assert key.request_users.request_id == USER_REQUEST_ID
    assert key.request_users.max_quantity == 1
    assert key.request_users.user_is_bot is False
    assert markup.one_time_keyboard


# ----------------------------------------------------------------- delete


async def test_delete_confirm_and_remove(service, access):
    await access.add_moderator(MODERATOR_ID, "Пётр", ADMIN_ID)
    confirm = await service.execute_callback(
        data=build_callback_data(ACTION_DELETE, MODERATOR_ID), user_id=ADMIN_ID
    )
    assert "Удалить Пётр?" in confirm.text
    assert _texts(confirm.keyboard) == ["Да, удалить", "Отмена"]
    assert access.is_moderator(MODERATOR_ID)

    done = await service.execute_callback(
        data=build_callback_data(ACTION_DELETE_CONFIRM, MODERATOR_ID), user_id=ADMIN_ID
    )
    assert not access.is_moderator(MODERATOR_ID)
    assert done.notice == "Модератор удалён"
    assert "Модераторов пока нет." in done.text


async def test_stale_delete_buttons(service, access):
    for action in (ACTION_DELETE, ACTION_DELETE_CONFIRM):
        reply = await service.execute_callback(
            data=build_callback_data(action, MODERATOR_ID), user_id=ADMIN_ID
        )
        assert reply.notice == "Модератор уже удалён"
        assert reply.text and reply.keyboard is not None


async def test_delete_cancel_returns_to_list(service, access):
    await access.add_moderator(MODERATOR_ID, "Пётр", ADMIN_ID)
    reply = await service.execute_callback(
        data=build_callback_data(ACTION_OPEN), user_id=ADMIN_ID
    )
    assert access.is_moderator(MODERATOR_ID)
    assert "Пётр" in reply.text


async def test_invalid_payload(service):
    reply = await service.execute_callback(data="mod", user_id=ADMIN_ID)
    assert reply.alert


# ----------------------------------------------------------------- report


async def test_report_screen_default_owner(service, settings):
    reply = await service.execute_callback(
        data=build_callback_data(ACTION_REPORT), user_id=ADMIN_ID
    )
    assert "каждый день в 09:00 (время София)" in reply.text
    texts = _texts(reply.keyboard)
    assert "✅ Владелец Иван (владелец)" in texts
    assert "⬜ Мария" in texts
    assert "📨 Прислать сводку за сегодня" in texts
    assert "⬅ К модераторам" in texts


async def test_toggle_on_off_and_empty(service, store, settings):
    on = await service.execute_callback(
        data=build_callback_data(ACTION_TOGGLE, SECOND_ADMIN_ID, 1), user_id=ADMIN_ID
    )
    assert store.get_report_recipients(settings) == (ADMIN_ID, SECOND_ADMIN_ID)
    assert "✅ Мария" in _texts(on.keyboard)

    await service.execute_callback(
        data=build_callback_data(ACTION_TOGGLE, ADMIN_ID, 0), user_id=ADMIN_ID
    )
    off = await service.execute_callback(
        data=build_callback_data(ACTION_TOGGLE, SECOND_ADMIN_ID, 0), user_id=ADMIN_ID
    )
    assert store.get_report_recipients(settings) == ()
    assert "Никто не получает сводку" in off.text
    assert "⬜ Владелец Иван (владелец)" in _texts(off.keyboard)


async def test_stale_toggle(service, store, settings):
    same = await service.execute_callback(
        data=build_callback_data(ACTION_TOGGLE, ADMIN_ID, 1), user_id=ADMIN_ID
    )
    assert same.notice == "Уже так и настроено"
    gone = await service.execute_callback(
        data=build_callback_data(ACTION_TOGGLE, STRANGER_ID, 1), user_id=ADMIN_ID
    )
    assert "больше не администратор" in gone.notice
    assert store.get_report_recipients(settings) == (ADMIN_ID,)


async def test_report_label_cached_and_fallback(service, bot):
    bot.names.pop(SECOND_ADMIN_ID)
    first = await service.show_report()
    calls = bot.chat_calls
    await service.show_report()
    assert "⬜ ID %d" % SECOND_ADMIN_ID in _texts(first.keyboard)
    assert bot.chat_calls == calls + 1  # only the failed lookup is retried


async def test_send_today_goes_only_to_presser(service, bot, repository):
    reply = await service.execute_callback(
        data=build_callback_data(ACTION_TODAY), user_id=SECOND_ADMIN_ID
    )
    assert "отправлена" in reply.notice
    assert [m.chat_id for m in bot.sent] == [SECOND_ADMIN_ID]
    assert "21.10.2026" in bot.sent[0].text


async def test_non_admin_cannot_toggle_or_send(service, bot, store, settings):
    for data in (
        build_callback_data(ACTION_TOGGLE, STRANGER_ID, 1),
        build_callback_data(ACTION_TODAY),
    ):
        reply = await service.execute_callback(data=data, user_id=STRANGER_ID)
        assert reply.alert
    assert bot.sent == []
    assert store.get_report_recipients(settings) == (ADMIN_ID,)


# ------------------------------------------------------------------ /start


async def test_start_texts_for_three_roles(service, access):
    await access.add_moderator(MODERATOR_ID, "", ADMIN_ID)
    admin_text = service.start_text(ADMIN_ID)
    assert admin_text.startswith("Вы администратор. Команды: /sources, /settings, /moderators")
    assert f"<code>{ADMIN_ID}</code>" in admin_text
    moderator_text = service.start_text(MODERATOR_ID)
    assert moderator_text.startswith("Вы модератор.")
    assert f"<code>{MODERATOR_ID}</code>" in moderator_text
    stranger = service.start_text(STRANGER_ID)
    assert f"<code>{STRANGER_ID}</code>" in stranger
    assert "администратору канала" in stranger
    assert "/sources" not in stranger


async def test_start_handler_answers(service):
    message = FakeAnswerMessage(from_user=FakeUser(STRANGER_ID))
    await handle_start(message, service)
    assert str(STRANGER_ID) in message.answers[0][0]


# ----------------------------------------------------------------- wiring


def test_router_registered_in_dispatcher(service, settings):
    from news_monitor.telegram.bot import create_dispatcher

    class Stub:
        access = None

    dispatcher = create_dispatcher(Stub(), settings, None, None, None, service)
    names = [router.name for router in dispatcher.sub_routers]
    assert names.index("moderators-admin") < names.index("moderation")
