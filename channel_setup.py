"""Bounded, owner-configured channel creation using the saved cloud session.

The command is supplied only through Render environment configuration. There
is no public command endpoint. Results contain channel metadata, never an
account session, access hash, password, phone number or private invite link.
"""

import asyncio
from collections import Counter
from copy import copy
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession


ROLLOUT_DELAY_SECONDS = 150
PREFIX = "CHANNEL_SETUP_RESULT "


class ChannelJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["create_channel"]
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(min_length=1, max_length=128)
    about: str = Field(min_length=1, max_length=255)
    username_candidates: list[str] = Field(min_length=1, max_length=6)
    created_after: AwareDatetime
    expires_at: AwareDatetime
    allow_create: bool = True
    existing_channel_id: int | None = Field(default=None, gt=0)
    archive_existing_dialogs: bool = False
    keep_archived_on_new_messages: bool = True
    leave_unavailable_channels: bool = False

    @field_validator("username_candidates")
    @classmethod
    def validate_usernames(cls, values):
        if len(values) != len(set(value.casefold() for value in values)):
            raise ValueError("Duplicate username candidates")
        if any(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value) is None for value in values):
            raise ValueError("Invalid username")
        return values

    @model_validator(mode="after")
    def validate_window(self):
        window = self.expires_at - self.created_after
        if not timedelta(minutes=3) <= window <= timedelta(hours=1):
            raise ValueError("Invalid execution window")
        if self.title.strip() != self.title or not self.title.strip():
            raise ValueError("Invalid title")
        if self.existing_channel_id is not None and self.allow_create:
            raise ValueError("Existing-channel commands must not create another channel")
        return self


class SetupStopped(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def utc_now():
    return datetime.now(timezone.utc)


def check_window(job, now):
    if now > job.expires_at:
        raise SetupStopped("JOB_EXPIRED")
    if now + timedelta(seconds=30) < job.created_after:
        raise SetupStopped("JOB_NOT_STARTED")


def report(payload):
    print(PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def recent_owned_broadcast(channel, job):
    if not isinstance(channel, types.Channel):
        return False
    if not channel.creator or not channel.broadcast or channel.title != job.title:
        return False
    if job.existing_channel_id is not None:
        return channel.id == job.existing_channel_id
    date = channel.date
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date >= job.created_after


async def full_channel(client, channel):
    full = await client(functions.channels.GetFullChannelRequest(channel=channel))
    actual = next((item for item in full.chats
                   if isinstance(item, types.Channel) and item.id == channel.id), None)
    if actual is None:
        raise SetupStopped("CHANNEL_METADATA_UNAVAILABLE")
    return actual, full.full_chat


async def find_owned_match(client, job):
    matches = []
    async for dialog in client.iter_dialogs(limit=None, ignore_migrated=True):
        channel = dialog.entity
        if not recent_owned_broadcast(channel, job):
            continue
        actual, full = await full_channel(client, channel)
        if full.about == job.about and actual.creator and actual.broadcast:
            matches.append(actual)
    if len(matches) > 1:
        raise SetupStopped("MULTIPLE_MATCHING_CHANNELS")
    if matches:
        return matches[0]
    return None


async def ensure_channel(client, job, *, emit=report, now=utc_now):
    """Create one channel or recover the same owned channel after a restart."""
    check_window(job, now())
    channel = await find_owned_match(client, job)
    created = channel is None
    if created:
        if not job.allow_create:
            raise SetupStopped("RECOVERY_NOT_FOUND")
        # Check the public-channel limit before creating anything.
        await client(functions.channels.GetAdminedPublicChannelsRequest(check_limit=True))
        check_window(job, now())
        emit({"job_id": job.job_id, "phase": "creating", "title": job.title})
        updates = await client(functions.channels.CreateChannelRequest(
            title=job.title, about=job.about, broadcast=True, megagroup=False))
        candidates = [item for item in updates.chats
                      if isinstance(item, types.Channel) and item.broadcast and item.creator]
        if len(candidates) != 1:
            raise SetupStopped("CREATE_RESPONSE_AMBIGUOUS")
        channel = candidates[0]
        emit({"job_id": job.job_id, "phase": "created", "channel_id": channel.id,
              "title": channel.title})

    username_problem = None
    if not channel.username:
        for username in job.username_candidates:
            check_window(job, now())
            try:
                available = await client(functions.channels.CheckUsernameRequest(
                    channel=channel, username=username))
                if not available:
                    continue
                await client(functions.channels.UpdateUsernameRequest(channel=channel,
                                                                      username=username))
                break
            except (errors.UsernameInvalidError, errors.UsernameOccupiedError,
                    errors.UsernamePurchaseAvailableError):
                continue
            except errors.ChannelsAdminPublicTooMuchError:
                username_problem = "PUBLIC_CHANNEL_LIMIT"
                break
        else:
            username_problem = "NO_AVAILABLE_USERNAME"

    actual, full = await full_channel(client, channel)
    if (not actual.creator or not actual.broadcast or actual.title != job.title
            or full.about != job.about):
        raise SetupStopped("CHANNEL_VERIFICATION_FAILED")
    username = actual.username or ""
    return {"job_id": job.job_id, "phase": "done", "verified": True,
            "created": created, "channel_id": actual.id, "title": actual.title,
            "username": username, "public": bool(username),
            "url": f"https://t.me/{username}" if username else None,
            "notice": (username_problem or "USERNAME_NOT_APPLIED") if not username else None}


async def archive_other_dialogs(client, job, keep_channel_id, *, emit=report, now=utc_now):
    """Archive main-list dialogs while keeping the newly created channel visible."""
    moved = {}
    failed = {}
    retention_enabled = False
    retention_notice = None
    if job.keep_archived_on_new_messages:
        try:
            current = await client(functions.account.GetGlobalPrivacySettingsRequest())
            if not current.keep_archived_unmuted:
                updated = copy(current)
                updated.keep_archived_unmuted = True
                current = await client(functions.account.SetGlobalPrivacySettingsRequest(settings=updated))
            retention_enabled = bool(current.keep_archived_unmuted)
        except errors.FloodWaitError:
            raise
        except errors.RPCError as exc:
            retention_notice = type(exc).__name__

    def keep(dialog):
        return (isinstance(dialog.entity, types.Channel)
                and dialog.entity.id == keep_channel_id)

    async def main_dialogs():
        return [dialog async for dialog in client.iter_dialogs(folder=0, limit=None, ignore_migrated=True)]

    def peer_kind(peer):
        if isinstance(peer, types.User):
            if getattr(peer, "self", False):
                return "saved_messages"
            if peer.id == 777000:
                return "telegram_service"
            return "bot" if peer.bot else "personal_dialog"
        if isinstance(peer, types.ChannelForbidden):
            return "unavailable_channel"
        if isinstance(peer, types.Channel):
            return "group" if peer.megagroup else "channel"
        return "group"

    bad_peer_errors = (errors.ChannelInvalidError, errors.ChannelPrivateError,
                       errors.ChatIdInvalidError, errors.FolderIdInvalidError,
                       errors.PeerIdInvalidError)
    for _ in range(3):
        check_window(job, now())
        dialogs = await main_dialogs()
        targets = [dialog.entity for dialog in dialogs
                   if not keep(dialog) and utils.get_peer_id(dialog.entity) not in failed]
        if not targets:
            break
        for offset in range(0, len(targets), 50):
            check_window(job, now())
            batch = targets[offset:offset + 50]
            try:
                await client.edit_folder(batch, folder=1)
                moved.update({utils.get_peer_id(peer): peer_kind(peer) for peer in batch})
            except bad_peer_errors:
                # A single unsupported/system peer must not block all other dialogs.
                for peer in batch:
                    try:
                        await client.edit_folder(peer, folder=1)
                        moved[utils.get_peer_id(peer)] = peer_kind(peer)
                    except bad_peer_errors as exc:
                        kind = "saved_messages" if getattr(peer, "self", False) else "unsupported_peer"
                        failed[utils.get_peer_id(peer)] = {"kind": kind, "reason": type(exc).__name__}
            emit({"job_id": job.job_id, "phase": "archiving", "archived": len(moved),
                  "unsupported": len(failed)})

    remaining = [dialog for dialog in await main_dialogs() if not keep(dialog)]
    remaining_ids = {utils.get_peer_id(dialog.entity) for dialog in remaining}
    confirmed = {peer_id: kind for peer_id, kind in moved.items() if peer_id not in remaining_ids}
    remaining_kinds = dict(Counter(peer_kind(dialog.entity) for dialog in remaining))
    return {"archived": len(confirmed), "remaining": len(remaining), "verified": not remaining,
            "personal_dialogs_archived": sum(kind == "personal_dialog" for kind in confirmed.values()),
            "remaining_personal_dialogs": remaining_kinds.get("personal_dialog", 0),
            "remaining_kinds": remaining_kinds,
            "unsupported": len(failed), "unsupported_kinds": sorted({item["kind"] for item in failed.values()}),
            "retention_enabled": retention_enabled, "retention_notice": retention_notice}


async def leave_unavailable_channels(client, job, keep_channel_id, *, emit=report, now=utc_now):
    """Leave only channels explicitly reported inaccessible by Telegram."""
    targets = []
    async for dialog in client.iter_dialogs(limit=None, ignore_migrated=True):
        channel = dialog.entity
        if (isinstance(channel, types.ChannelForbidden) and channel.broadcast
                and channel.id != keep_channel_id):
            targets.append(channel)
    left = 0
    already_out = 0
    failed = Counter()
    for channel in targets:
        check_window(job, now())
        try:
            await client(functions.channels.LeaveChannelRequest(
                channel=types.InputChannel(channel.id, channel.access_hash)))
            left += 1
        except (errors.UserNotParticipantError, errors.ChannelPrivateError):
            already_out += 1
        except errors.FloodWaitError:
            raise
        except errors.RPCError as exc:
            failed[type(exc).__name__] += 1
        emit({"job_id": job.job_id, "phase": "leaving_unavailable", "left": left,
              "already_out": already_out, "failed": sum(failed.values())})
    return {"found": len(targets), "left": left, "already_out": already_out,
            "failed": sum(failed.values()), "failure_kinds": dict(failed), "verified": not failed}


async def run_configured_setup(raw, flow, *, decrypt, client_factory=TelegramClient,
                               sleep=asyncio.sleep, emit=report, now=utc_now):
    """Run once per process after the old Render instance has shut down.

    A finite execution window and recovery checks protect against repeated
    creation on free-service restarts. The operator clears the job afterwards.
    """
    if not raw.strip():
        return
    job = None
    client = None

    def publish(payload):
        flow.channel_setup_result = payload
        emit(payload)

    try:
        if len(raw) > 4096:
            raise SetupStopped("INVALID_JOB")
        job = ChannelJob.model_validate_json(raw)
        check_window(job, now())
        settings = flow.settings
        if not settings.api_ready or not settings.password_ready or not settings.encrypted_session:
            raise SetupStopped("SAVED_SESSION_REQUIRED")
        publish({"job_id": job.job_id, "phase": "waiting_for_rollout",
                 "delay_seconds": ROLLOUT_DELAY_SECONDS})
        await sleep(ROLLOUT_DELAY_SECONDS)
        check_window(job, now())
        async with flow.lock:
            # Only one Telegram connection is used in this process during setup.
            await flow.close()
            flow.client = None
            flow.authorized = False
            session = await asyncio.to_thread(decrypt, settings.encrypted_session,
                                              settings.dashboard_password)
            client = client_factory(
                StringSession(session), int(settings.api_id), settings.api_hash,
                device_model="Personal Channel Manager on Render", app_version="0.2.0",
                receive_updates=False, catch_up=False, request_retries=0,
                connection_retries=1, flood_sleep_threshold=0, auto_reconnect=False)
            try:
                async with asyncio.timeout(180):
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise SetupStopped("SAVED_SESSION_NOT_AUTHORIZED")
                    publish({"job_id": job.job_id, "phase": "authorized"})
                    result = await ensure_channel(client, job, emit=publish, now=now)
                    if job.leave_unavailable_channels:
                        publish({**result, "phase": "channel_ready"})
                        result["unavailable_channels"] = await leave_unavailable_channels(
                            client, job, result["channel_id"], emit=publish, now=now)
                    if job.archive_existing_dialogs:
                        publish({**result, "phase": "channel_ready"})
                        result["archive"] = await archive_other_dialogs(
                            client, job, result["channel_id"], emit=publish, now=now)
                    publish(result)
            finally:
                await client.disconnect()
                client = None
    except asyncio.CancelledError:
        raise
    except errors.FloodWaitError as exc:
        publish({"job_id": job.job_id if job else None, "phase": "stopped",
                 "reason": "TELEGRAM_FLOOD_WAIT", "retry_after": max(1, int(exc.seconds))})
    except SetupStopped as exc:
        publish({"job_id": job.job_id if job else None, "phase": "stopped", "reason": exc.reason})
    except Exception as exc:
        # Exception text can contain user input or provider request parameters.
        publish({"job_id": job.job_id if job else None, "phase": "error",
                 "reason": type(exc).__name__})
    finally:
        if client is not None:
            await client.disconnect()
