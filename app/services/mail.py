"""Send registration codes and lecture notifications via SMTP."""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import AudioFile, LectureStatus, User

logger = logging.getLogger(__name__)


def _send_email(settings: Settings, recipient: str, subject: str, body: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise OSError("SMTP host and sender are required")
    if settings.smtp_security == "plain" and (settings.smtp_username or settings.smtp_password):
        raise OSError("SMTP authentication requires TLS")

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate()
    message["Message-ID"] = make_msgid(domain=settings.smtp_from.rsplit("@", 1)[1])
    message.set_content(body)
    tls = ssl.create_default_context()
    connection: smtplib.SMTP
    if settings.smtp_security == "ssl":
        connection = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=8, context=tls
        )
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=8)
    with connection:
        if settings.smtp_security == "starttls":
            connection.starttls(context=tls)
        if settings.smtp_username:
            connection.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        connection.send_message(message)


def send_registration_code(settings: Settings, recipient: str, code: str) -> None:
    _send_email(
        settings,
        recipient,
        "Код регистрации EduFlow",
        f"Код регистрации EduFlow: {code}\n\nКод действует 10 минут.\n",
    )


async def notify_lecture_owner(
    session: AsyncSession, settings: Settings, lecture: AudioFile
) -> None:
    """Notify the owner after the lecture outcome has been committed."""
    if not settings.smtp_host or not settings.smtp_from:
        return

    file_id = lecture.id
    filename = lecture.filename or f"Лекция {str(file_id)[:8]}"
    successful = lecture.status == LectureStatus.completed
    try:
        owner = await session.get(User, lecture.user_id)
        recipient = owner.email if owner is not None else None
        # Release the read transaction before waiting on the mail server.
        await session.rollback()
    except SQLAlchemyError as exc:
        logger.warning(
            "Lecture %s notification recipient unavailable (%s)", file_id, type(exc).__name__
        )
        return
    if recipient is None:
        return

    if successful:
        subject = "LectureAI — лекция готова"
        detail = "Расшифровка, конспект и карточки доступны в разделе «Мои лекции»."
    else:
        subject = "LectureAI — не удалось обработать лекцию"
        detail = "Проверь статус в разделе «Мои лекции». Там можно загрузить запись заново."

    try:
        await asyncio.to_thread(
            _send_email, settings, recipient, subject, f"{filename}\n\n{detail}\n"
        )
    except (OSError, ValueError) as exc:
        logger.warning("Lecture %s notification failed (%s)", file_id, type(exc).__name__)
