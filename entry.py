import base64
import io
import json
import os
import posixpath
import re
import zipfile
from datetime import datetime, timezone

from workers import WorkerEntrypoint, Response
from js import fetch, Object
from pyodide.ffi import to_js


# =========================================================
# CONFIG
# =========================================================

ADMIN_ID = 5943392316

GITHUB_OWNER = "MSR-Cracker"
GITHUB_REPO = "Bot-Hoster-Test"


def js_options(options):
    return to_js(options, dict_converter=Object.fromEntries)


async def api_json(url, method="GET", data=None, headers=None):
    opts = {
        "method": method,
        "headers": headers or {},
    }

    if data is not None:
        opts["headers"]["Content-Type"] = "application/json"
        opts["body"] = json.dumps(data, ensure_ascii=False)

    response = await fetch(
        url,
        js_options(opts)
    )

    text = await response.text()

    try:
        body = json.loads(text)
    except Exception:
        body = {"raw": text}

    return response.status, body


async def telegram_call(token, method, data):
    return await api_json(
        f"https://api.telegram.org/bot{token}/{method}",
        "POST",
        data,
        {
            "Content-Type": "application/json",
        },
    )


async def github_call(token, method, path, data=None):
    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}{path}"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Telegram-Hosting-Worker",
    }

    return await api_json(
        url,
        method,
        data,
        headers
    )


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def safe_name(value, fallback="bot"):
    value = re.sub(
        r"[^A-Za-z0-9_-]+",
        "-",
        value.strip()
    )

    value = re.sub(
        r"-+",
        "-",
        value
    )

    value = value.strip("-_")

    return (value or fallback)[:60]


def safe_zip_path(name):
    name = name.replace("\\", "/").lstrip("/")

    normalized = posixpath.normpath(name)

    if not normalized or normalized == ".":
        return None

    if normalized.startswith("../"):
        return None

    if normalized == "..":
        return None

    if "/../" in normalized:
        return None

    if normalized.startswith("/"):
        return None

    return normalized


class Default(WorkerEntrypoint):

    # =====================================================
    # MAIN REQUEST
    # =====================================================

    async def fetch(self, request):

        if request.method != "POST":
            return Response(
                "Telegram Hosting Bot is running.",
                status=200
            )

        try:
            update = await request.json()

            await self.handle_update(update)

        except Exception as e:
            print("ERROR:", repr(e))

        return Response(
            "OK",
            status=200
        )

    # =====================================================
    # TELEGRAM
    # =====================================================

    async def send(self, chat_id, text, **kwargs):

        data = {
            "chat_id": chat_id,
            "text": text,
        }

        data.update(kwargs)

        return await telegram_call(
            self.env.BOT_TOKEN,
            "sendMessage",
            data
        )

    async def answer_callback(self, callback_id):

        await telegram_call(
            self.env.BOT_TOKEN,
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
            },
        )

    # =====================================================
    # GITHUB STORAGE
    # =====================================================

    async def get_repo_file(self, path):

        status, body = await github_call(
            self.env.GITHUB_TOKEN,
            "GET",
            "/contents/" + path,
        )

        if status != 200:
            return None, None

        content = base64.b64decode(
            body["content"].replace("\n", "")
        )

        return (
            content.decode("utf-8"),
            body.get("sha")
        )

    async def put_repo_file(
        self,
        path,
        content,
        message,
        sha=None
    ):

        payload = {
            "message": message,
            "content": base64.b64encode(
                content.encode("utf-8")
            ).decode("ascii"),
        }

        if sha:
            payload["sha"] = sha

        status, body = await github_call(
            self.env.GITHUB_TOKEN,
            "PUT",
            "/contents/" + path,
            payload,
        )

        if status not in (200, 201):
            raise RuntimeError(
                f"GitHub write failed: HTTP {status}: {body}"
            )

        return body

    async def read_json(self, path, default):

        text, _ = await self.get_repo_file(path)

        if not text:
            return default

        try:
            return json.loads(text)

        except Exception:
            return default

    async def write_json(
        self,
        path,
        obj,
        message
    ):

        old, sha = await self.get_repo_file(path)

        await self.put_repo_file(
            path,
            json.dumps(
                obj,
                ensure_ascii=False,
                indent=2
            ),
            message,
            sha,
        )

    async def get_users(self):

        return await self.read_json(
            "data/users.json",
            {}
        )

    async def get_pending(self):

        return await self.read_json(
            "data/pending.json",
            {}
        )

    async def get_hosts(self):

        return await self.read_json(
            "data/hosts.json",
            {}
        )

    # =====================================================
    # USERS
    # =====================================================

    async def ensure_user(self, user):

        users = await self.get_users()

        uid = str(user["id"])

        if uid not in users:

            users[uid] = {
                "telegram_id": user["id"],
                "username": user.get("username") or "",
                "hosting_limit": 1,
                "created_at": now_iso(),
            }

        else:

            users[uid]["username"] = (
                user.get("username")
                or users[uid].get("username", "")
            )

        await self.write_json(
            "data/users.json",
            users,
            f"Register/update user {uid}",
        )

    async def get_user(self, user_id):

        users = await self.get_users()

        return users.get(
            str(user_id)
        )

    async def set_limit(
        self,
        user_id,
        limit
    ):

        users = await self.get_users()

        uid = str(user_id)

        if uid not in users:

            users[uid] = {
                "telegram_id": user_id,
                "username": "",
                "hosting_limit": limit,
                "created_at": now_iso(),
            }

        else:

            users[uid]["hosting_limit"] = limit

        await self.write_json(
            "data/users.json",
            users,
            f"Set hosting limit for {uid}",
        )

    async def usage(self, user_id):

        hosts = await self.get_hosts()

        return sum(
            1
            for h in hosts.values()
            if str(h.get("user_id")) == str(user_id)
        )

    # =====================================================
    # ZIP VALIDATION
    # =====================================================

    def inspect_zip(self, raw):

        max_files = int(
            os.getenv(
                "MAX_ZIP_FILES",
                "100"
            )
        )

        max_uncompressed = int(
            os.getenv(
                "MAX_UNCOMPRESSED_BYTES",
                str(50 * 1024 * 1024)
            )
        )

        try:

            with zipfile.ZipFile(
                io.BytesIO(raw)
            ) as z:

                infos = z.infolist()

                if len(infos) > max_files:

                    return (
                        False,
                        "عدد الملفات داخل ZIP كبير جداً.",
                        []
                    )

                total = 0
                entries = []

                for info in infos:

                    p = safe_zip_path(
                        info.filename
                    )

                    if not p:

                        return (
                            False,
                            f"مسار غير آمن: {info.filename}",
                            []
                        )

                    # Reject symlinks
                    mode = (
                        info.external_attr >> 16
                    ) & 0o170000

                    if mode == 0o120000:

                        return (
                            False,
                            f"Symlink غير مسموح: {info.filename}",
                            []
                        )

                    total += info.file_size

                    if total > max_uncompressed:

                        return (
                            False,
                            "الحجم بعد فك الضغط تجاوز الحد.",
                            []
                        )

                    entries.append(p)

                return (
                    True,
                    "OK",
                    entries
                )

        except zipfile.BadZipFile:

            return (
                False,
                "الملف ليس ZIP صالحاً.",
                []
            )

        except Exception as e:

            return (
                False,
                f"فشل فحص ZIP: {e}",
                []
            )

    # =====================================================
    # UPDATE HANDLER
    # =====================================================

    async def handle_update(self, update):

        callback = update.get(
            "callback_query"
        )

        if callback:

            await self.handle_callback(
                callback
            )

            return

        message = update.get(
            "message"
        ) or {}

        user = message.get(
            "from"
        ) or {}

        chat = message.get(
            "chat"
        ) or {}

        user_id = user.get(
            "id"
        )

        chat_id = chat.get(
            "id"
        )

        if not user_id or not chat_id:
            return

        await self.ensure_user(
            user
        )

        text = message.get(
            "text"
        ) or ""

        # /start
        if text.startswith("/start"):

            await self.home(
                chat_id,
                user_id
            )

            return

        # /setlimit USER_ID LIMIT
        if text.startswith("/setlimit"):

            if not self.is_admin(
                user_id
            ):
                return

            parts = text.split()

            if (
                len(parts) != 3
                or not parts[1].isdigit()
                or not parts[2].isdigit()
            ):

                await self.send(
                    chat_id,
                    "الاستخدام:\n"
                    "/setlimit USER_ID LIMIT"
                )

                return

            target = int(
                parts[1]
            )

            limit = int(
                parts[2]
            )

            await self.set_limit(
                target,
                limit
            )

            await self.send(
                chat_id,
                f"✅ تم تعيين حد المستخدم `{target}` إلى {limit}.",
                parse_mode="Markdown",
            )

            return

        # /user USER_ID
        if text.startswith("/user"):

            if not self.is_admin(
                user_id
            ):
                return

            parts = text.split()

            if (
                len(parts) != 2
                or not parts[1].isdigit()
            ):

                await self.send(
                    chat_id,
                    "الاستخدام:\n"
                    "/user USER_ID"
                )

                return

            await self.admin_user(
                chat_id,
                int(parts[1])
            )

            return

        # ZIP upload
        document = message.get(
            "document"
        )

        if document:

            if (
                await self.get_state(user_id)
                != "waiting_upload"
            ):

                await self.send(
                    chat_id,
                    "اضغط ➕ إنشاء استضافة أولاً."
                )

                return

            await self.receive_zip(
                message,
                user
            )

            return

        await self.home(
            chat_id,
            user_id
        )

    # =====================================================
    # HOME
    # =====================================================

    async def home(
        self,
        chat_id,
        user_id=None
    ):

        buttons = [
            [
                {
                    "text": "➕ إنشاء استضافة بوت",
                    "callback_data": "create"
                }
            ],
            [
                {
                    "text": "🤖 بوتاتي",
                    "callback_data": "mybots"
                }
            ],
            [
                {
                    "text": "🗑 حذف استضافة",
                    "callback_data": "delete"
                }
            ],
        ]

        if (
            user_id is not None
            and self.is_admin(user_id)
        ):

            buttons.append([
                {
                    "text": "🛡️ لوحة الأدمن",
                    "callback_data": "admin"
                }
            ])

        await self.send(
            chat_id,
            "👋 أهلاً بك في بوت الاستضافة!\n\n"
            "من هنا تقدر تنشئ وتدير استضافات بوتاتك.",
            reply_markup={
                "inline_keyboard": buttons
            }
        )

    # =====================================================
    # CALLBACKS
    # =====================================================

    async def handle_callback(
        self,
        callback
    ):

        await self.answer_callback(
            callback.get("id")
        )

        user = callback.get(
            "from"
        ) or {}

        user_id = user.get(
            "id"
        )

        message = callback.get(
            "message"
        ) or {}

        chat_id = (
            message.get("chat")
            or {}
        ).get("id")

        data = callback.get(
            "data"
        ) or ""

        if not user_id or not chat_id:
            return

        await self.ensure_user(
            user
        )

        if data == "home":

            await self.home(
                chat_id,
                user_id
            )

        elif data == "create":

            limit = (
                await self.get_user(
                    user_id
                )
            )["hosting_limit"]

            used = await self.usage(
                user_id
            )

            if used >= limit:

                await self.send(
                    chat_id,
                    f"⚠️ وصلت للحد الأقصى.\n\n"
                    f"الحد: {limit}\n"
                    f"المستخدم: {used}",
                )

                return

            await self.set_state(
                user_id,
                "waiting_upload"
            )

            await self.send(
                chat_id,
                "📦 أرسل ملف البوت الآن بصيغة ZIP.\n\n"
                "⚠️ الملف سيتم إرساله للأدمن للمراجعة أولاً، "
                "ولن يتم اعتماده قبل موافقته.",
            )

        elif data == "mybots":

            await self.mybots(
                chat_id,
                user_id
            )

        elif data == "delete":

            await self.delete_menu(
                chat_id,
                user_id
            )

        elif data.startswith("del:"):

            await self.delete_confirm(
                chat_id,
                user_id,
                data.split(
                    ":",
                    1
                )[1]
            )

        elif data.startswith("delconfirm:"):

            await self.delete_host(
                chat_id,
                user_id,
                data.split(
                    ":",
                    1
                )[1]
            )

        elif data.startswith("approve:"):

            if self.is_admin(user_id):

                await self.approve(
                    chat_id,
                    data.split(
                        ":",
                        1
                    )[1]
                )

        elif data.startswith("reject:"):

            if self.is_admin(user_id):

                await self.reject(
                    chat_id,
                    data.split(
                        ":",
                        1
                    )[1]
                )

        elif data == "admin":

            if self.is_admin(user_id):

                await self.admin_panel(
                    chat_id
                )

    # =====================================================
    # ADMIN PANEL
    # =====================================================

    async def admin_panel(
        self,
        chat_id
    ):

        pending = await self.get_pending()

        pending_count = sum(
            1
            for x in pending.values()
            if x.get("status") == "pending"
        )

        users = await self.get_users()

        hosts = await self.get_hosts()

        await self.send(
            chat_id,
            "🛡️ لوحة تحكم الأدمن\n\n"
            f"👥 المستخدمين: {len(users)}\n"
            f"🤖 الاستضافات: {len(hosts)}\n"
            f"⏳ الطلبات المعلقة: {pending_count}\n\n"
            "لتغيير حد مستخدم:\n"
            "/setlimit USER_ID LIMIT\n\n"
            "لعرض بيانات مستخدم:\n"
            "/user USER_ID",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "📋 الطلبات المعلقة",
                            "callback_data": "pending"
                        }
                    ],
                    [
                        {
                            "text": "🔙 رجوع",
                            "callback_data": "home"
                        }
                    ]
                ]
            }
        )

    # =====================================================
    # UPLOAD
    # =====================================================

    async def receive_zip(
        self,
        message,
        user
    ):

        user_id = user["id"]

        chat_id = (
            message.get("chat")
            or {}
        ).get("id")

        document = (
            message.get("document")
            or {}
        )

        file_id = document.get(
            "file_id"
        )

        filename = (
            document.get("file_name")
            or "bot.zip"
        )

        file_size = int(
            document.get("file_size")
            or 0
        )

        max_size = int(
            os.getenv(
                "MAX_UPLOAD_BYTES",
                str(10 * 1024 * 1024)
            )
        )

        if not filename.lower().endswith(
            ".zip"
        ):

            await self.send(
                chat_id,
                "❌ ارفع ملف ZIP فقط."
            )

            await self.set_state(
                user_id,
                None
            )

            return

        if file_size > max_size:

            await self.send(
                chat_id,
                "❌ الملف أكبر من الحجم المسموح."
            )

            await self.set_state(
                user_id,
                None
            )

            return

        # Get Telegram file
        info = await telegram_call(
            self.env.BOT_TOKEN,
            "getFile",
            {
                "file_id": file_id
            },
        )

        if not info.get("ok"):

            await self.send(
                chat_id,
                "❌ تعذر تنزيل الملف من Telegram."
            )

            await self.set_state(
                user_id,
                None
            )

            return

        response = await fetch(
            f"https://api.telegram.org/file/"
            f"bot{self.env.BOT_TOKEN}/"
            f"{info['result']['file_path']}"
        )

        raw = bytes(
            (
                await response.arrayBuffer()
            ).to_py()
        )

        # Inspect ZIP
        valid, reason, entries = self.inspect_zip(
            raw
        )

        if not valid:

            await self.send(
                chat_id,
                f"❌ تم رفض الملف مبدئياً:\n{reason}"
            )

            await self.set_state(
                user_id,
                None
            )

            return

        pending = await self.get_pending()

        request_id = str(
            max(
                [
                    int(x)
                    for x in pending.keys()
                ]
                or [0]
            ) + 1
        )

        project = safe_name(
            filename[:-4],
            "bot"
        )

        pending[request_id] = {
            "id": int(request_id),
            "user_id": user_id,
            "username": user.get("username") or "",
            "filename": filename,
            "project_name": project,
            "file_id": file_id,
            "file_size": file_size,
            "file_count": len(entries),
            "status": "pending",
            "created_at": now_iso(),
        }

        await self.write_json(
            "data/pending.json",
            pending,
            f"Create pending request {request_id}",
        )

        admin_text = (
            "🚨 طلب استضافة جديد\n\n"
            f"👤 المستخدم: @{user.get('username') or 'بدون username'}\n"
            f"🆔 ID: `{user_id}`\n"
            f"📦 الملف: `{filename}`\n"
            f"📏 الحجم: {file_size} bytes\n"
            f"📁 عدد الملفات: {len(entries)}\n\n"
            "🔎 اجتاز الفحص الأولي.\n"
            "⚠️ راجع محتوى الملف بنفسك قبل Confirm."
        )

        sent = await telegram_call(
            self.env.BOT_TOKEN,
            "sendDocument",
            {
                "chat_id": ADMIN_ID,
                "document": file_id,
                "caption": admin_text,
                "parse_mode": "Markdown",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": "✅ Confirm",
                                "callback_data": f"approve:{request_id}"
                            },
                            {
                                "text": "❌ Reject",
                                "callback_data": f"reject:{request_id}"
                            }
                        ]
                    ]
                }
            },
        )

        if not sent.get("ok"):

            pending[request_id]["status"] = "failed"

            await self.write_json(
                "data/pending.json",
                pending,
                f"Mark request {request_id} failed",
            )

            await self.send(
                chat_id,
                "❌ فشل إرسال الملف للأدمن."
            )

            return

        await self.set_state(
            user_id,
            None
        )

        await self.send(
            chat_id,
            "⏳ تم إرسال الملف للأدمن للمراجعة.\n"
            "انتظر قرار الإدارة."
        )

    # =====================================================
    # APPROVE
    # =====================================================

    async def approve(
        self,
        admin_chat_id,
        request_id
    ):

        pending = await self.get_pending()

        req = pending.get(
            str(request_id)
        )

        if (
            not req
            or req.get("status") != "pending"
        ):

            await self.send(
                admin_chat_id,
                "⚠️ الطلب غير موجود أو تمت معالجته."
            )

            return

        user_id = req["user_id"]

        user = await self.get_user(
            user_id
        )

        limit = int(
            user.get(
                "hosting_limit",
                1
            )
        ) if user else 1

        used = await self.usage(
            user_id
        )

        if used >= limit:

            await self.send(
                admin_chat_id,
                f"❌ لا يمكن الاعتماد: المستخدم وصل للحد ({limit})."
            )

            return

        # Re-download
        info = await telegram_call(
            self.env.BOT_TOKEN,
            "getFile",
            {
                "file_id": req["file_id"]
            },
        )

        if not info.get("ok"):

            await self.send(
                admin_chat_id,
                "❌ تعذر تنزيل الملف من Telegram."
            )

            return

        response = await fetch(
            f"https://api.telegram.org/file/"
            f"bot{self.env.BOT_TOKEN}/"
            f"{info['result']['file_path']}"
        )

        raw = bytes(
            (
                await response.arrayBuffer()
            ).to_py()
        )

        valid, reason, _ = self.inspect_zip(
            raw
        )

        if not valid:

            await self.send(
                admin_chat_id,
                f"❌ فشل الفحص مرة أخرى:\n{reason}"
            )

            return

        hosts = await self.get_hosts()

        host_id = f"{user_id}-{request_id}"

        root = (
            f"hosts/{user_id}/{host_id}"
        )

        uploaded = 0

        try:

            with zipfile.ZipFile(
                io.BytesIO(raw)
            ) as z:

                for info in z.infolist():

                    if info.is_dir():
                        continue

                    path = safe_zip_path(
                        info.filename
                    )

                    if not path:
                        raise ValueError(
                            "Unsafe ZIP path"
                        )

                    await self.put_github_binary(
                        f"{root}/{path}",
                        z.read(info),
                        f"Add host {host_id}: {path}",
                    )

                    uploaded += 1

            hosts[host_id] = {
                "id": host_id,
                "user_id": user_id,
                "name": req["project_name"],
                "github_path": root,
                "status": "approved",
                "created_at": now_iso(),
            }

            await self.write_json(
                "data/hosts.json",
                hosts,
                f"Approve host {host_id}",
            )

            req["status"] = "approved"

            req["approved_at"] = now_iso()

            await self.write_json(
                "data/pending.json",
                pending,
                f"Approve request {request_id}",
            )

            await self.send(
                admin_chat_id,
                f"✅ تم اعتماد الاستضافة ورفع {uploaded} ملف إلى GitHub.\n\n"
                f"👤 User: `{user_id}`\n"
                f"🤖 Bot: `{req['project_name']}`",
                parse_mode="Markdown",
            )

            await self.send(
                user_id,
                f"✅ تم قبول استضافتك!\n\n"
                f"🤖 البوت: {req['project_name']}\n"
                f"📦 الملفات: {uploaded}\n"
                "📌 الحالة: Approved",
            )

        except Exception as e:

            print(
                "APPROVE ERROR:",
                repr(e)
            )

            await self.send(
                admin_chat_id,
                "❌ فشل رفع الملفات إلى GitHub:\n"
                f"{str(e)[:1000]}"
            )

    # =====================================================
    # REJECT
    # =====================================================

    async def reject(
        self,
        admin_chat_id,
        request_id
    ):

        pending = await self.get_pending()

        req = pending.get(
            str(request_id)
        )

        if (
            not req
            or req.get("status") != "pending"
        ):

            await self.send(
                admin_chat_id,
                "⚠️ الطلب غير موجود أو تمت معالجته."
            )

            return

        req["status"] = "rejected"

        req["rejected_at"] = now_iso()

        await self.write_json(
            "data/pending.json",
            pending,
            f"Reject request {request_id}",
        )

        await self.send(
            admin_chat_id,
            "❌ تم رفض الطلب."
        )

        await self.send(
            req["user_id"],
            "❌ تم رفض طلب الاستضافة من الإدارة."
        )

    # =====================================================
    # GITHUB BINARY
    # =====================================================

    async def put_github_binary(
        self,
        path,
        content,
        message
    ):

        payload = {
            "message": message,
            "content": base64.b64encode(
                content
            ).decode("ascii"),
        }

        status, body = await github_call(
            self.env.GITHUB_TOKEN,
            "PUT",
            "/contents/" + path,
            payload,
        )

        if status not in (
            200,
            201
        ):

            raise RuntimeError(
                f"GitHub upload failed: "
                f"HTTP {status}: {body}"
            )

    # =====================================================
    # DELETE GITHUB FILES
    # =====================================================

    async def delete_github_tree(
        self,
        root
    ):

        status, body = await github_call(
            self.env.GITHUB_TOKEN,
            "GET",
            "/contents/" + root,
        )

        if status == 404:
            return

        if status != 200:

            raise RuntimeError(
                f"GitHub list failed: {body}"
            )

        for item in body:

            if item["type"] == "file":

                status2, body2 = await github_call(
                    self.env.GITHUB_TOKEN,
                    "DELETE",
                    "/contents/" + item["path"],
                    {
                        "message": f"Delete {item['path']}",
                        "sha": item["sha"],
                    },
                )

                if status2 not in (
                    200,
                    204
                ):

                    raise RuntimeError(
                        f"GitHub delete failed: {body2}"
                    )

            elif item["type"] == "dir":

                await self.delete_github_tree(
                    item["path"]
                )

    # =====================================================
    # MY BOTS
    # =====================================================

    async def mybots(
        self,
        chat_id,
        user_id
    ):

        hosts = await self.get_hosts()

        mine = [
            h
            for h in hosts.values()
            if str(h.get("user_id"))
            == str(user_id)
        ]

        if not mine:

            await self.send(
                chat_id,
                "🤖 لا توجد استضافات لديك حالياً."
            )

            return

        text = "🤖 بوتاتك:\n\n"

        buttons = []

        for i, host in enumerate(
            mine,
            1
        ):

            text += (
                f"{i}. "
                f"{host['name']} — "
                f"{host['status']}\n"
            )

            buttons.append([
                {
                    "text": f"🗑 حذف {host['name']}",
                    "callback_data": f"del:{host['id']}"
                }
            ])

        buttons.append([
            {
                "text": "🔙 رجوع",
                "callback_data": "home"
            }
        ])

        await self.send(
            chat_id,
            text,
            reply_markup={
                "inline_keyboard": buttons
            }
        )

    # =====================================================
    # DELETE MENU
    # =====================================================

    async def delete_menu(
        self,
        chat_id,
        user_id
    ):

        hosts = await self.get_hosts()

        mine = [
            h
            for h in hosts.values()
            if str(h.get("user_id"))
            == str(user_id)
        ]

        if not mine:

            await self.send(
                chat_id,
                "🗑 لا توجد استضافات لحذفها."
            )

            return

        buttons = [
            [
                {
                    "text": f"🗑 {h['name']}",
                    "callback_data": f"del:{h['id']}"
                }
            ]
            for h in mine
        ]

        buttons.append([
            {
                "text": "🔙 رجوع",
                "callback_data": "home"
            }
        ])

        await self.send(
            chat_id,
            "اختر الاستضافة التي تريد حذفها:",
            reply_markup={
                "inline_keyboard": buttons
            }
        )

    # =====================================================
    # DELETE CONFIRM
    # =====================================================

    async def delete_confirm(
        self,
        chat_id,
        user_id,
        host_id
    ):

        hosts = await self.get_hosts()

        host = hosts.get(
            host_id
        )

        if (
            not host
            or str(host.get("user_id"))
            != str(user_id)
        ):

            await self.send(
                chat_id,
                "❌ الاستضافة غير موجودة."
            )

            return

        await self.send(
            chat_id,
            f"⚠️ هل تريد حذف `{host['name']}`؟",
            parse_mode="Markdown",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ نعم، احذف",
                            "callback_data":
                                f"delconfirm:{host_id}"
                        },
                        {
                            "text": "❌ إلغاء",
                            "callback_data": "home"
                        }
                    ]
                ]
            }
        )

    # =====================================================
    # DELETE HOST
    # =====================================================

    async def delete_host(
        self,
        chat_id,
        user_id,
        host_id
    ):

        hosts = await self.get_hosts()

        host = hosts.get(
            host_id
        )

        if (
            not host
            or str(host.get("user_id"))
            != str(user_id)
        ):

            await self.send(
                chat_id,
                "❌ الاستضافة غير موجودة."
            )

            return

        try:

            await self.delete_github_tree(
                host["github_path"]
            )

            del hosts[host_id]

            await self.write_json(
                "data/hosts.json",
                hosts,
                f"Delete host {host_id}",
            )

            await self.send(
                chat_id,
                f"✅ تم حذف استضافة "
                f"{host['name']} "
                f"وملفاتها من GitHub."
            )

        except Exception as e:

            print(
                "DELETE ERROR:",
                repr(e)
            )

            await self.send(
                chat_id,
                "❌ حدث خطأ أثناء حذف الاستضافة:\n"
                f"{str(e)[:1000]}"
            )

    # =====================================================
    # ADMIN
    # =====================================================

    def is_admin(
        self,
        user_id
    ):

        return str(user_id) == str(
            ADMIN_ID
        )

    async def admin_user(
        self,
        chat_id,
        user_id
    ):

        user = await self.get_user(
            user_id
        )

        if not user:

            await self.send(
                chat_id,
                "❌ المستخدم غير موجود."
            )

            return

        used = await self.usage(
            user_id
        )

        limit = int(
            user.get(
                "hosting_limit",
                1
            )
        )

        await self.send(
            chat_id,
            f"👤 المستخدم\n\n"
            f"🆔 `{user_id}`\n"
            f"Username: @{user.get('username') or 'بدون username'}\n"
            f"📦 الحد: {limit}\n"
            f"📊 المستخدم: {used}\n"
            f"🟢 المتبقي: {max(0, limit - used)}",
            parse_mode="Markdown",
        )

    # =====================================================
    # STATE
    # =====================================================

    async def get_state(
        self,
        user_id
    ):

        states = await self.read_json(
            "data/states.json",
            {}
        )

        return states.get(
            str(user_id)
        )

    async def set_state(
        self,
        user_id,
        state
    ):

        states = await self.read_json(
            "data/states.json",
            {}
        )

        if state is None:

            states.pop(
                str(user_id),
                None
            )

        else:

            states[str(user_id)] = state

        await self.write_json(
            "data/states.json",
            states,
            f"Update state {user_id}",
        )
