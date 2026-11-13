"""stored translations: carry the copy fixes of NP4a and since to rows that still hold the old text

A deploy only ever added NEW keys (`app.seed`); stored rows were never rewritten, so corrected copy
never reached the live site. This migration rewrites a row **only where its value still equals a
previous repo default** (the locale texts of 914da9a, 1088f12, 78c90df and 36713b4, i.e. before
NP4a) -- a therapist's own edit holds other text and survives -- and adds the keys that did not
exist then. Additive and idempotent; the downgrade does nothing (the old text is the therapist's
to bring back, and a rewrite of her words is not undone by a schema step).

The data AND the mechanism are embedded: the migration reads neither the locale files, nor git, nor
any app code (a later edit of `app.core.services.translation_defaults`, which newer slices reuse
for their own copy fixes, must not change what this revision does on a fresh database).

Revision ID: 0008_translation_copy_fixes
Revises: 0007_unverified_holds
Create Date: 2026-11-14 12:00:00.000000
"""

# ruff: noqa: E501, RUF001
from __future__ import annotations

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_translation_copy_fixes"
down_revision: str | None = "0007_unverified_holds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (lang, key, previous repo defaults, new default)
Change = tuple[str, str, Sequence[str], str]
#: (lang, key, default)
Addition = tuple[str, str, str]

CHANGES: tuple[Change, ...] = (
    ("en", "auth.greeting_unnamed", ("Hello, client.",), "Hello."),
    ("en", "auth.link_sent", ("If that address is known here, a sign-in link is on its way.",), "Check your inbox. If this address is known here, a link is on its way — it can take a minute."),
    ("en", "common.error.not_found", ("That was not found.",), "We could not find that page."),
    ("en", "common.home", ("Home",), "Back to the home page"),
    ("en", "intent.auth.login_link.client.books", ("Someone chose {when} ({session_type}) using your email address. If that was you, open this link and send the booking request. It works once and expires in {minutes} minutes.",), "Someone chose {when} ({session_type}) using your email address. If that was you, open this link and send the booking request."),
    ("hy", "auth.greeting_unnamed", ("Բարև Ձեզ, հարգելի հաճախորդ։",), "Բարև Ձեզ։"),
    ("hy", "auth.link_sent", ("Եթե այդ հասցեն հայտնի է այստեղ, մուտքի հղումն ուղարկվել է։",), "Ստուգեք փոստը։ Եթե այս հասցեն ինձ հայտնի է, մուտքի հղումն արդեն ճանապարհին է․ երբեմն մեկ րոպե է պետք։"),
    ("hy", "booking.cancelled_by_user", ("Ամրագրումը չեղարկվեց: Դուք վերադարձել եք գլխավոր մենյու:",), "Ամրագրումը չեղարկվեց։ Դուք վերադարձել եք գլխավոր մենյու։"),
    ("hy", "booking.captcha_wait", ("Մեկ վայրկյան — ավարտում ենք արագ ստուգումը։",), "Մեկ վայրկյան — ավարտում եմ արագ ստուգումը։"),
    ("hy", "booking.check_email", ("Մուտքի հղումը ուղարկվել է ձեր փոստին։ Անցեք այդ հղումով՝ այս հայտը բացելու և հիշեցումներ ստանալու համար։",), "Մուտքի հղումն ուղարկվել է ձեր փոստին։ Անցեք այդ հղումով՝ այս հայտը բացելու և հիշեցումներ ստանալու համար։"),
    ("hy", "booking.choose_timezone", ("Նշեք ձեր ժամային գոտին:",), "Նշեք ձեր ժամային գոտին։"),
    ("hy", "booking.contact.email_invalid", ("Սա էլ. փոստի հասցեի նման չէ։ Խնդրում ենք ստուգել և նորից ուղարկել։",), "Սա էլ. փոստի հասցեի նման չէ։ Խնդրում եմ ստուգել և նորից ուղարկել։"),
    ("hy", "booking.contact.email_taken", ("Այդ հասցեն արդեն կապված է այլ պրոֆիլի հետ։ Խնդրում ենք օգտագործել մեկ ուրիշը։",), "Այդ հասցեն արդեն կապված է այլ պրոֆիլի հետ։ Խնդրում եմ օգտագործել մեկ ուրիշը։"),
    ("hy", "booking.slot.held", ("Խնդրում ենք ավարտել {minutes} րոպեի ընթացքում։ Ժամը ձեզ համար չի պահվում, մինչև հայտը չուղարկեք, ուստի այն դեռ կարող է ընտրել մեկ ուրիշը։",), "Խնդրում եմ ավարտել {minutes} րոպեի ընթացքում։ Ժամը ձեզ համար չի պահվում, մինչև հայտը չուղարկեք, ուստի այն դեռ կարող է ընտրել մեկ ուրիշը։"),
    ("hy", "booking.slot.hold_expired", ("Ձեր ամրագրման ժամկետը լրացավ։ Խնդրում ենք նորից ընտրել ժամանակ։",), "Ձեր ամրագրման ժամկետը լրացավ։ Խնդրում եմ նորից ընտրել ժամանակ։"),
    ("hy", "booking.slot.taken", ("Ցավոք, այդ ժամանակը արդեն զբաղված է: Խնդրում ենք ընտրել այլ ժամանակ:",), "Ցավոք, այդ ժամը արդեն զբաղված է։ Խնդրում եմ ընտրել այլ ժամ։"),
    ("hy", "booking.timezone.detected", ("Հայտնաբերվեց {timezone}։ Ճի՞շտ է։",), "Ժամերը ցուցադրված են ձեր ժամային գոտով։ Եթե դա այդպես չէ, ընտրեք մեկ այլ գոտի։"),
    ("hy", "common.error.expired_link", ("Այս հղումն այլևս վավեր չէ։ Խնդրում ենք նորը պահանջել։",), "Այս հղումն այլևս վավեր չէ։ Խնդրում եմ նորը պահանջել։"),
    ("hy", "common.error.generic", ("Տեղի է ունեցել սխալ:",), "Ինչ-որ բան չստացվեց։ Խնդրում եմ փորձել մի փոքր ուշ։"),
    ("hy", "common.error.not_found", ("Չի գտնվել։",), "Այդ էջը չգտա։ Հնարավոր է՝ հղումն հնացել է։"),
    ("hy", "common.error.rate_limited", ("Չափազանց շատ փորձեր։ Խնդրում ենք փորձել մի փոքր ուշ։",), "Չափազանց շատ փորձեր։ Խնդրում եմ փորձել մի փոքր ուշ։"),
    ("hy", "common.home", ("Գլխավոր",), "Վերադառնալ"),
    ("hy", "common.welcome", ("Բարի գալուստ:",), "Բարի գալուստ։"),
    ("hy", "common.welcome_back", ("Դուք վերադարձել եք գլխավոր մենյու:",), "Դուք վերադարձել եք գլխավոր մենյու։"),
    ("hy", "content.empty", ("Տեղեկատվությունը դեռ ավելացված չէ:",), "Տեղեկատվությունը դեռ ավելացված չէ։"),
    ("hy", "intent.auth.login_link.client.books", ("Ինչ-որ մեկը ընտրել է {when} ({session_type}) ձեր էլ. փոստի հասցեով։ Եթե դա դուք էիք, բացեք այս հղումը և ուղարկեք հայտը։ Այն գործում է մեկ անգամ և վավեր է {minutes} րոպե։",), "Ինչ-որ մեկը ընտրել է {when} ({session_type}) ձեր էլ. փոստի հասցեով։ Եթե դա դուք էիք, բացեք այս հղումը և ուղարկեք հայտը։"),
    ("hy", "lang.select", ("Խնդրում ենք ընտրել լեզուն:",), "Խնդրում եմ ընտրել լեզուն։"),
    ("hy", "merge.busy", ("Այստեղ ունեք անավարտ գործողություն։ Խնդրում ենք նախ ավարտել այն, ապա նորից բացել նամակի հղումը։",), "Այստեղ ունեք անավարտ գործողություն։ Խնդրում եմ նախ ավարտել այն, ապա նորից բացել նամակի հղումը։"),
    ("hy", "waitlist.ask_contact", ("Թողեք ձեր կոնտակտային տվյալները:",), "Թողեք ձեր կոնտակտային տվյալները։"),
    ("hy", "waitlist.intro", ("Ցավոք, այս պահին ազատ տեղեր չկան: Կարող եք գրանցվել սպասման ցուցակում:",), "Ցավոք, այս պահին ազատ տեղեր չկան։ Կարող եք գրանցվել սպասման ցուցակում։"),
    ("ru", "auth.greeting_unnamed", ("Здравствуйте, уважаемый клиент.",), "Здравствуйте."),
    ("ru", "auth.link_sent", ("Если этот адрес здесь известен, ссылка для входа уже отправлена.",), "Проверьте почту. Если этот адрес мне известен, ссылка для входа уже в пути — иногда она идёт минуту."),
    ("ru", "booking.slot.none_online", ("Свободных онлайн-времён сейчас нет. Очные есть.",), "Сейчас нет свободного времени для онлайн-встреч. Есть время для очных."),
    ("ru", "booking.slot.none_onsite", ("Свободных очных времён сейчас нет. Онлайн есть.",), "Сейчас нет свободного времени для очных встреч. Есть время для онлайн-встреч."),
    ("ru", "booking.submitted", ("Спасибо. Ваш запрос отправлен, номер {uuid}. Я свяжусь с вами в ближайшее время.",), "Спасибо. Ваша заявка отправлена, номер {uuid}. Я свяжусь с вами в ближайшее время."),
    ("ru", "booking.timezone.detected", ("Определён часовой пояс {timezone}. Всё верно?",), "Время показано в вашем часовом поясе. Если это не так, выберите другой."),
    ("ru", "booking.unavailable", ("Сейчас запись закрыта.",), "Сейчас приём заявок закрыт."),
    ("ru", "common.error.not_found", ("Не найдено.",), "Такой страницы нет. Возможно, ссылка устарела."),
    ("ru", "common.home", ("Домой",), "На главную"),
    ("ru", "intent.auth.login_link.client.books", ("Кто-то выбрал {when} ({session_type}), указав ваш адрес электронной почты. Если это были вы, откройте ссылку и отправьте заявку. Ссылка действует один раз, {minutes} минут.",), "Кто-то выбрал {when} ({session_type}), указав ваш адрес электронной почты. Если это были вы, откройте ссылку и отправьте заявку."),
    ("ru", "merge.confirm", ("У вас здесь есть запись, сделанная по электронной почте. Присоединить её к этому Telegram-аккаунту, чтобы всё было в одном месте?",), "У вас здесь есть заявка, поданная по электронной почте. Присоединить её к этому Telegram-аккаунту, чтобы всё было в одном месте?"),
    ("ru", "merge.done", ("Готово. Теперь все ваши записи в одном месте.",), "Готово. Теперь все ваши заявки в одном месте."),
    ("ru", "waitlist.from_picker", ("Ни одно из этих времён мне не подходит",), "Мне не подходит ни один вариант"),
    ("ru", "waitlist.intro", ("К сожалению, сейчас нет свободных мест. Вы можете оставить заявку в лист ожидания.",), "Сейчас у меня нет свободного времени. Оставьте заявку в листе ожидания — я напишу, когда оно появится."),
)

#: (lang, key, default) -- keys that did not exist before NP4a
ADDITIONS: tuple[Addition, ...] = (
    ("en", "auth.confirm_book_contact", "Contact: {contact}"),
    ("en", "auth.confirm_book_name", "Name on the request: {name}"),
    ("en", "booking.choose_another", "Choose another time"),
    ("en", "booking.choose_slot_web", "Choose a time that suits you."),
    ("en", "booking.contact.email_throttled", "Several links have already been sent to that address. Please try again in an hour, or skip this step."),
    ("en", "booking.email_hint", "Only for the confirmation link and reminders."),
    ("en", "booking.onsite_no_address", "I will agree the address with you."),
    ("en", "booking.slot.taken_title", "That time is taken"),
    ("en", "booking.timezone.shown", "Times are shown in {zone}."),
    ("en", "common.change", "Change"),
    ("en", "common.error.expired_link_title", "This link no longer works"),
    ("en", "common.error.not_found_title", "Page not found"),
    ("en", "common.error.stale", "This page was open for too long, so the form was not sent. Your text is still here — please press the button again."),
    ("en", "common.error.title", "Something went wrong"),
    ("en", "common.noscript", "Choosing a time needs JavaScript. Please turn it on and reload the page."),
    ("en", "common.optional", "(optional)"),
    ("en", "common.send_request", "Send request"),
    ("en", "common.skip_to_content", "Skip to the content"),
    ("en", "common.step", "Step {n} of {total}"),
    ("en", "request.decline_ask", "Decline? This closes the request."),
    ("en", "request.decline_yes", "Yes, decline"),
    ("en", "request.reference", "Reference: {ref}"),
    ("en", "request.you", "You"),
    ("en", "waitlist.join", "Join the waiting list"),
    ("hy", "auth.confirm_book_contact", "Կոնտակտ՝ {contact}"),
    ("hy", "auth.confirm_book_name", "Անունը հայտում՝ {name}"),
    ("hy", "booking.choose_another", "Ընտրել այլ ժամ"),
    ("hy", "booking.choose_slot_web", "Ընտրեք ձեզ հարմար ժամը։"),
    ("hy", "booking.contact.email_throttled", "Այդ հասցեին արդեն մի քանի հղում է ուղարկվել։ Փորձեք մեկ ժամից կամ բաց թողեք այս քայլը։"),
    ("hy", "booking.email_hint", "Միայն հաստատման հղման և հիշեցումների համար։"),
    ("hy", "booking.onsite_no_address", "Հասցեն կհամաձայնեցնեմ ձեզ հետ։"),
    ("hy", "booking.slot.taken_title", "Այդ ժամը զբաղված է"),
    ("hy", "booking.timezone.shown", "Ժամերը ցուցադրված են՝ {zone}։"),
    ("hy", "common.change", "Փոխել"),
    ("hy", "common.error.expired_link_title", "Հղումն այլևս չի գործում"),
    ("hy", "common.error.not_found_title", "Էջը չի գտնվել"),
    ("hy", "common.error.stale", "Էջը չափազանց երկար բաց է մնացել, ուստի ձևը չուղարկվեց։ Ձեր տեքստը պահպանված է․ սեղմեք կոճակը նորից։"),
    ("hy", "common.error.title", "Ինչ-որ բան չստացվեց"),
    ("hy", "common.noscript", "Ժամն ընտրելու համար անհրաժեշտ է JavaScript։ Խնդրում եմ միացնել այն և թարմացնել էջը։"),
    ("hy", "common.optional", "(ըստ ցանկության)"),
    ("hy", "common.send_request", "Ուղարկել հայտը"),
    ("hy", "common.skip_to_content", "Անցնել բովանդակությանը"),
    ("hy", "common.step", "Քայլ {n}՝ {total}-ից"),
    ("hy", "request.decline_ask", "Հրաժարվե՞լ։ Հայտը կփակվի։"),
    ("hy", "request.decline_yes", "Այո, հրաժարվել"),
    ("hy", "request.reference", "Հայտի համարը՝ {ref}"),
    ("hy", "request.you", "Դուք"),
    ("hy", "waitlist.join", "Գրանցվել սպասման ցուցակում"),
    ("ru", "auth.confirm_book_contact", "Контакт: {contact}"),
    ("ru", "auth.confirm_book_name", "Имя в заявке: {name}"),
    ("ru", "booking.choose_another", "Выбрать другое время"),
    ("ru", "booking.choose_slot_web", "Выберите удобное время."),
    ("ru", "booking.contact.email_throttled", "На этот адрес уже отправлено несколько ссылок. Попробуйте через час или пропустите этот шаг."),
    ("ru", "booking.email_hint", "Только для ссылки подтверждения и напоминаний."),
    ("ru", "booking.onsite_no_address", "Адрес я согласую с вами."),
    ("ru", "booking.slot.taken_title", "Это время уже занято"),
    ("ru", "booking.timezone.shown", "Время указано в поясе: {zone}."),
    ("ru", "common.change", "Изменить"),
    ("ru", "common.error.expired_link_title", "Ссылка не действует"),
    ("ru", "common.error.not_found_title", "Страница не найдена"),
    ("ru", "common.error.stale", "Страница была открыта слишком долго, поэтому форма не отправилась. Ваш текст сохранён — нажмите кнопку ещё раз."),
    ("ru", "common.error.title", "Что-то пошло не так"),
    ("ru", "common.noscript", "Чтобы выбрать время, нужен JavaScript. Включите его и обновите страницу."),
    ("ru", "common.optional", "(необязательно)"),
    ("ru", "common.send_request", "Отправить заявку"),
    ("ru", "common.skip_to_content", "Перейти к содержимому"),
    ("ru", "common.step", "Шаг {n} из {total}"),
    ("ru", "request.decline_ask", "Отказаться? Заявка будет закрыта."),
    ("ru", "request.decline_yes", "Да, отказаться"),
    ("ru", "request.reference", "Номер заявки: {ref}"),
    ("ru", "request.you", "Вы"),
    ("ru", "waitlist.join", "Встать в лист ожидания"),
)


def apply_default_changes(
    conn: sa.Connection, changes: Iterable[Change], additions: Iterable[Addition] = ()
) -> tuple[int, int]:
    """Rewrite a row only where it still holds a previous default; add the keys that are missing.

    Returns (rows rewritten, rows added). A frozen copy of the helper this revision was written
    with: idempotent, and a therapist's own edit is never touched.
    """
    rewrite = sa.text(
        "UPDATE translation SET value = :new, updated_at = now() "
        "WHERE lang = :lang AND key = :key AND value IN :old AND value <> :new"
    ).bindparams(sa.bindparam("old", expanding=True))
    add = sa.text(
        "INSERT INTO translation (practice_id, lang, key, value) "
        "SELECT id, :lang, :key, :value FROM practice "
        "ON CONFLICT (practice_id, lang, key) DO NOTHING"
    )
    rewritten = added = 0
    for lang, key, old, new in changes:
        result = conn.execute(rewrite, {"lang": lang, "key": key, "old": list(old), "new": new})
        rewritten += result.rowcount
    for lang, key, value in additions:
        result = conn.execute(add, {"lang": lang, "key": key, "value": value})
        added += result.rowcount
    return rewritten, added


def upgrade() -> None:
    apply_default_changes(op.get_bind(), CHANGES, ADDITIONS)


def downgrade() -> None:
    """Deliberately nothing."""
