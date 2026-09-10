"""私聊传声筒 Plus。

在 Napcat 适配器之上提供无限制的主动私聊转达能力（不修改适配器本体）：

- 通过 LLM 工具 ``send_private_chat`` 把消息转达给任意 QQ 用户，可反复调用实现多轮来回对话；
- 转达内容默认带上请求者称呼，用户要求匿名时由 Planner 改为转述口吻（工具约定中声明）；
- 每次发送结果即时反馈给工具调用方（Planner），由其转告请求者；
- 对方在私聊中回复后，自动把回复转告请求者所在的聊天流（提问类消息的结果回传）；
- 等待超时或任务到期时给出自然语言提示，并说明私聊名单过滤可能导致回复丢失。

发送走宿主统一发送管线（``ctx.send.text``），消息会正常入库并进入对方私聊流的上下文；
回复监听通过宿主 Hook ``chat.receive.after_process`` 完成；转告通过 Maisaka 主动回合
（``ctx.maisaka.proactive.trigger``）唤醒 Planner 人格化表达，失败时回退为纯文本发送。
"""

from __future__ import annotations

from collections import deque
from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParamType, ToolParameterInfo
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple

import asyncio
import json
import time

SUPPORTED_CONFIG_VERSION = "0.1.0"

# Napcat 适配器（官方插件）的插件 ID 与本插件会用到的公开 API
ADAPTER_PLUGIN_ID = "maibot-team.napcat-adapter"
API_LOGIN_INFO = "adapter.napcat.system.get_login_info"
API_FRIEND_LIST = "adapter.napcat.account.get_friend_list"
API_STRANGER_INFO = "adapter.napcat.account.get_stranger_info"
API_GROUP_MEMBER_LIST = "adapter.napcat.group.get_group_member_list"
API_GROUP_MEMBER_INFO = "adapter.napcat.group.get_group_member_info"

# 适配器配置中提供连接标识（scope）的配置段
ADAPTER_SCOPE_SECTION = "napcat_server"
ADAPTER_SCOPE_FIELD = "connection_id"

# 本地状态文件名（位于 ctx.paths.data_dir 下）
TASKS_FILE_NAME = "relay_tasks.json"

# 适配器路由身份（account_id + scope）的缓存秒数
ROUTE_CACHE_SECONDS = 300.0

# 发送频率限制的滑动窗口长度
RATE_LIMIT_WINDOW_SECONDS = 3600.0

# 名称解析候选最多返回数量
MAX_CANDIDATES = 5


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "forum"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class RelayConfig(PluginConfigBase):
    """回复转达配置。"""

    __ui_label__ = "回复转达"
    __ui_order__ = 1

    wait_reply_default: bool = Field(
        default=True,
        description="工具未显式指定 ask_reply 时，是否默认等待并转达对方回复",
    )
    reply_timeout_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="等待对方回复的超时时间（分钟），超时后通知请求者并结束任务",
    )
    max_task_lifetime_minutes: int = Field(
        default=240,
        ge=10,
        le=10080,
        description="单个转达任务的最长存活时间（分钟），防止任务无限驻留",
    )
    aggregate_seconds: int = Field(
        default=15,
        ge=3,
        le=120,
        description="对方连发多条消息时的合并等待秒数，避免刷屏",
    )
    notify_on_timeout: bool = Field(
        default=True,
        description="等待超时后是否通知请求者",
    )
    hint_filter_issue: bool = Field(
        default=True,
        description="超时提示中附带「私聊名单过滤可能拦截对方回复」的说明",
    )
    notify_via_planner: bool = Field(
        default=True,
        description="用 Maisaka 主动回合（Planner）转达回复与超时提示；关闭则直接发送纯文本",
    )


class SendConfig(PluginConfigBase):
    """发送与目标解析配置。"""

    __ui_label__ = "发送与解析"
    __ui_order__ = 2

    platform: str = Field(
        default="qq",
        description="目标平台标识（与适配器网关平台一致）",
    )
    use_friend_list: bool = Field(
        default=True,
        description="按昵称解析目标时查询 QQ 好友列表",
    )
    use_group_members: bool = Field(
        default=True,
        description="按昵称解析目标时查询当前群成员列表",
    )
    polish_content: bool = Field(
        default=False,
        description="发送前用 replyer 模型润色转达内容（口吻更自然，但会略微增加耗时）",
    )


class SecurityConfig(PluginConfigBase):
    """授权名单与频率限制配置。"""

    __ui_label__ = "安全与限流"
    __ui_order__ = 3

    mode: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        description=(
            "授权模式：whitelist 白名单（默认，仅名单内的用户/群可以使用传话工具）；"
            "blacklist 黑名单（名单内的用户/群被拒绝，其余人可用）"
        ),
    )
    users: List[str] = Field(
        default_factory=list,
        description="用户 QQ 名单：白名单模式下表示允许使用的用户；黑名单模式下表示拒绝使用的用户",
    )
    groups: List[str] = Field(
        default_factory=list,
        description="群号名单：白名单模式下表示允许使用的群；黑名单模式下表示拒绝使用的群（对群内所有人生效）",
    )
    max_sends_per_user_per_hour: int = Field(
        default=20,
        ge=0,
        le=1000,
        description="同一用户每小时最多发送条数，0 表示不限制",
    )
    max_sends_per_target_per_hour: int = Field(
        default=20,
        ge=0,
        le=1000,
        description="同一目标用户每小时全局最多被发送条数，0 表示不限制",
    )
    max_sends_total_per_hour: int = Field(
        default=100,
        ge=0,
        le=10000,
        description="全体用户每小时最多发送总条数，0 表示不限制",
    )
    enable_adapter_open_tool: bool = Field(
        default=False,
        description=(
            "是否在加载时启用 Napcat 适配器的 open_private_chat 工具。"
            "该操作会改动其他插件的安全相关组件状态（该工具首条消息可建立 15 分钟私聊临时放行），"
            "默认关闭，仅在明确知晓影响后开启。"
        ),
    )


class PrivateChatToolPlusConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    relay: RelayConfig = Field(default_factory=RelayConfig)
    send: SendConfig = Field(default_factory=SendConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)


class _RelayTask:
    """一条「等待对方回复并转告请求者」的任务。"""

    def __init__(
        self,
        *,
        target_user_id: str,
        target_display_name: str,
        target_stream_id: str,
        requester_user_id: str,
        requester_stream_id: str,
        created_at: float,
        expire_at: float,
        buffer: Optional[List[str]] = None,
    ) -> None:
        self.target_user_id = target_user_id
        self.target_display_name = target_display_name
        self.target_stream_id = target_stream_id
        self.requester_user_id = requester_user_id
        self.requester_stream_id = requester_stream_id
        self.created_at = created_at
        self.expire_at = expire_at
        # 待合并转达的回复文本（仅内存，刻意不落盘以保护隐私）
        self.buffer: List[str] = buffer if buffer is not None else []
        # 合并定时任务句柄（仅运行时）
        self.flush_delay_task: Optional["asyncio.Task[None]"] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可持久化的字典（刻意不含 buffer 回复正文，避免隐私内容落盘）。"""

        return {
            "target_user_id": self.target_user_id,
            "target_display_name": self.target_display_name,
            "target_stream_id": self.target_stream_id,
            "requester_user_id": self.requester_user_id,
            "requester_stream_id": self.requester_stream_id,
            "created_at": self.created_at,
            "expire_at": self.expire_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "_RelayTask":
        """从持久化字典恢复任务对象。"""

        # 持久化不含 buffer 回复正文；重启后未转达的缓冲视为已丢弃
        return cls(
            target_user_id=str(data.get("target_user_id") or ""),
            target_display_name=str(data.get("target_display_name") or ""),
            target_stream_id=str(data.get("target_stream_id") or ""),
            requester_user_id=str(data.get("requester_user_id") or ""),
            requester_stream_id=str(data.get("requester_stream_id") or ""),
            created_at=float(data.get("created_at") or time.time()),
            expire_at=float(data.get("expire_at") or time.time()),
        )


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """把 LLM 传入的布尔参数规范化为 bool。"""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on", "是"):
        return True
    if text in ("false", "0", "no", "off", "否"):
        return False
    return default


def _normalize_qq_id(value: Any) -> str:
    """把输入规范化为纯数字 QQ 号；不合法时返回空字符串。"""

    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 5:
        return ""
    return digits


class PrivateChatToolPlusPlugin(MaiBotPlugin):
    """私聊传声筒 Plus 主插件。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = PrivateChatToolPlusConfig

    def __init__(self) -> None:
        super().__init__()
        # 进行中的转达任务：key = 请求者聊天流|目标QQ
        self._tasks: Dict[str, _RelayTask] = {}
        # 每个任务对应的超时看门狗协程
        self._watchdogs: Dict[str, "asyncio.Task[None]"] = {}
        # 适配器登录账号 ID（懒加载缓存）
        self._bot_account_id: str = ""
        # 适配器路由身份缓存: (account_id, scope, cached_at)
        self._route_cache: Tuple[str, str, float] = ("", "", 0.0)
        # 发送频率记录: key("user:<qq>"/"target:<qq>"/"total") -> 时间戳队列（仅内存，重启清零）
        self._send_history: Dict[str, deque] = {}

    async def on_load(self) -> None:
        """加载插件：恢复持久化任务并尝试启用适配器的主动私聊工具。"""

        self._restore_tasks()
        await self._try_enable_adapter_tool()
        self.ctx.logger.info("私聊传声筒 Plus 已加载，当前进行中的转达任务 %d 个", len(self._tasks))

    async def on_unload(self) -> None:
        """卸载插件：取消全部后台任务并持久化状态。"""

        for watchdog in self._watchdogs.values():
            watchdog.cancel()
        self._watchdogs.clear()
        for task in self._tasks.values():
            if task.flush_delay_task is not None and not task.flush_delay_task.done():
                task.flush_delay_task.cancel()
        self._save_tasks()
        self.ctx.logger.info("私聊传声筒 Plus 已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新。"""

        if scope == "self":
            self.ctx.logger.info("私聊传声筒 Plus 配置已更新: version=%s", version)

    # ------------------------------------------------------------------
    # LLM 工具：send_private_chat
    # ------------------------------------------------------------------

    @Tool(
        "send_private_chat",
        brief_description="把一条消息通过 QQ 私聊转达给指定用户，可主动发起私聊并支持多轮来回对话",
        detailed_description=(
            "把消息转达给指定的 QQ 用户：主动开启或继续与对方的私聊，发送次数无限制，可用于多轮来回对话。\n"
            "使用约定：\n"
            "1. content 必须是完整、可直接发出的消息文本，用自然的口吻替当前用户转达。\n"
            "2. 默认在消息中带上当前用户的称呼（如「XX让我问你……」）；只有当前用户明确要求不要透露是谁让传的"
            "（例如「别说是我说的」「不要说是我讲的」）时，才改为以自己的口吻转述，不提来源。\n"
            "3. 当消息中包含需要对方回答的问题时，把 ask_reply 设为 true；对方回复后系统会自动把回复转告当前用户。\n"
            "4. 不知道对方 QQ 号时填 target_name（昵称/群名片/备注），系统会自动解析；"
            "解析出多个候选时结果会列出候选名单，请先向当前用户确认，不要擅自猜测。\n"
            "5. 需要继续对话时，可以直接再次调用本工具给同一个人发新消息；每次调用的发送结果都会反馈给你。"
        ),
        parameters=[
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="要发给对方的完整消息文本",
                required=True,
            ),
            ToolParameterInfo(
                name="target_id",
                param_type=ToolParamType.STRING,
                description="目标用户的 QQ 号（纯数字）",
                required=False,
            ),
            ToolParameterInfo(
                name="target_name",
                param_type=ToolParamType.STRING,
                description="目标用户的昵称/群名片/备注，不知道 QQ 号时填写",
                required=False,
            ),
            ToolParameterInfo(
                name="ask_reply",
                param_type=ToolParamType.BOOLEAN,
                description="消息中包含需要对方回答的问题时设为 true；对方回复后会自动转告当前用户",
                required=False,
            ),
        ],
    )
    async def tool_send_private_chat(
        self,
        content: str = "",
        target_id: str = "",
        target_name: str = "",
        ask_reply: Any = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """把消息转达给目标用户，并按需注册回复转达任务。"""

        text = str(content or "").strip()
        if not text:
            return {"success": False, "content": "content 不能为空：请写明要转达给对方的完整消息。"}

        requester_stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        requester_user_id = str(kwargs.get("user_id") or "").strip()
        group_id = str(kwargs.get("group_id") or "").strip()
        platform = str(kwargs.get("platform") or "").strip() or self.config.send.platform

        # 0. 授权检查：白名单/私聊/群范围
        if not self._is_authorized_requester(requester_user_id, group_id):
            self.ctx.logger.info(
                "私聊传话请求被拒绝（未授权）: 请求者=%s 群=%s",
                requester_user_id or "<unknown>",
                group_id or "<private>",
            )
            return {
                "success": False,
                "content": (
                    "当前用户没有使用私聊传话功能的权限。"
                    "如需调整，请管理员在插件配置的 security 段修改授权模式（mode）与名单（users/groups）。"
                ),
            }

        # 1. 解析目标用户（QQ 号或按名字解析）
        resolved = await self._resolve_target(target_id=target_id, target_name=target_name, group_id=group_id)
        if not resolved["ok"]:
            result: Dict[str, Any] = {"success": False, "content": str(resolved["error"])}
            if resolved.get("candidates"):
                result["candidates"] = resolved["candidates"]
            return result

        target_user_id: str = resolved["user_id"]
        target_display: str = resolved["display_name"] or target_user_id

        # 2. 基础校验：不给机器人自己发私聊
        bot_account_id = await self._get_bot_account_id()
        if bot_account_id and target_user_id == bot_account_id:
            return {"success": False, "content": "目标用户是机器人自己，无法私聊。"}

        # 3. 频率限制（按请求者、按目标、全局）
        rate_error = self._check_rate_limit(requester_user_id, target_user_id)
        if rate_error:
            return {"success": False, "content": rate_error}

        # 4. 可选的内容润色（默认关闭）
        if self.config.send.polish_content:
            requester_name = await self._resolve_requester_name(requester_user_id, group_id)
            text = await self._polish_content(text, requester_name)

        # 5. 定位或创建目标用户的私聊会话
        target_stream_id = ""
        existing_stream = await self._find_existing_private_stream(target_user_id, platform)
        if existing_stream:
            target_stream_id = str(existing_stream.get("session_id") or "").strip()
        else:
            account_id, scope_id = await self._get_adapter_route_identity()
            try:
                open_result = await self.ctx.chat.open_session(
                    platform=platform,
                    chat_type="private",
                    user_id=target_user_id,
                    account_id=account_id,
                    scope=scope_id,
                )
            except Exception as exc:
                return {"success": False, "content": f"打开与「{target_display}」的私聊会话失败: {exc}"}
            if not isinstance(open_result, dict) or not bool(open_result.get("success", False)):
                error = str(open_result.get("error") or "").strip() if isinstance(open_result, dict) else ""
                return {
                    "success": False,
                    "content": f"打开与「{target_display}」的私聊会话失败: {error or '未知原因'}",
                }
            target_stream_id = str(open_result.get("session_id") or open_result.get("stream_id") or "").strip()

        if not target_stream_id:
            return {"success": False, "content": f"无法定位「{target_display}」的私聊会话，发送中止。"}

        # 6. 通过宿主统一发送管线发送（正常入库、进入对方私聊流上下文）
        try:
            send_result = await self.ctx.send.text(text, target_stream_id, return_details=True)
        except Exception as exc:
            return {"success": False, "content": f"向「{target_display}」发送私聊消息时异常: {exc}"}
        sent = bool(send_result.get("sent")) if isinstance(send_result, dict) else bool(send_result)
        if not sent:
            return {
                "success": False,
                "content": (
                    f"消息未能发送给「{target_display}」（QQ:{target_user_id}）。"
                    "可能原因：适配器未连接或私聊路由不可用，详情请查看主程序日志。"
                ),
            }

        self.ctx.logger.info(
            "已转达私聊消息: 目标=%s(%s) 请求者流=%s 字数=%d",
            target_display,
            target_user_id,
            requester_stream_id or "<unknown>",
            len(text),
        )

        # 7. 按需注册回复转达任务
        wait_reply = _coerce_bool(ask_reply, default=self.config.relay.wait_reply_default)
        task_registered = False
        if wait_reply and requester_stream_id:
            self._register_task(
                target_user_id=target_user_id,
                target_display_name=target_display,
                target_stream_id=target_stream_id,
                requester_user_id=requester_user_id,
                requester_stream_id=requester_stream_id,
            )
            task_registered = True

        feedback = f"已把消息转达给「{target_display}」（QQ:{target_user_id}）。"
        if task_registered:
            feedback += (
                f"接下来约 {self.config.relay.reply_timeout_minutes} 分钟内对方在私聊里回复的话，"
                "会自动转告当前用户；期间对方继续回复也会持续转达。"
            )
            if not existing_stream:
                feedback += (
                    "注意：对方此前与机器人没有私聊会话，若适配器开启了私聊名单过滤，"
                    "需要管理员把对方加入白名单或关闭该过滤，对方的回复才能送达。"
                )
        else:
            feedback += "本次不等待对方回复。"

        return {
            "success": True,
            "content": feedback,
            "target_user_id": target_user_id,
            "target_display_name": target_display,
            "target_stream_id": target_stream_id,
            "message_id": str(send_result.get("message_id") or "") if isinstance(send_result, dict) else "",
            "wait_reply": task_registered,
        }

    # ------------------------------------------------------------------
    # Hook：监听目标用户的私聊回复
    # ------------------------------------------------------------------

    @HookHandler(
        "chat.receive.after_process",
        name="private_chat_reply_watcher",
        mode=HookMode.OBSERVE,
        description="监听私聊消息，把转达目标用户的回复合并后转告请求者",
    )
    async def watch_private_chat_replies(self, **kwargs: Any) -> None:
        """观察入站私聊消息，命中转达任务时进入合并缓冲。"""

        message = kwargs.get("message")
        if not isinstance(message, dict) or not self._tasks:
            return
        if message.get("is_notify") or message.get("is_command"):
            return

        message_info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        group_info = message_info.get("group_info") if isinstance(message_info.get("group_info"), dict) else {}
        user_info = message_info.get("user_info") if isinstance(message_info.get("user_info"), dict) else {}

        # 只关心私聊消息
        if str(group_info.get("group_id") or "").strip():
            return

        sender_id = str(user_info.get("user_id") or "").strip()
        if not sender_id or (self._bot_account_id and sender_id == self._bot_account_id):
            return

        text = str(message.get("processed_plain_text") or "").strip()
        if not text:
            # 图片/表情等非文本消息给出占位描述
            if bool(message.get("is_picture")):
                text = "[图片]"
            elif bool(message.get("is_emoji")):
                text = "[表情包]"
        if not text:
            return

        session_id = str(message.get("session_id") or "").strip()
        now = time.time()
        for key, task in list(self._tasks.items()):
            if task.target_user_id != sender_id or now >= task.expire_at:
                continue
            # 会话流不一致时跳过（避免同号异常场景误转达）
            if session_id and task.target_stream_id and session_id != task.target_stream_id:
                continue
            try:
                self._buffer_reply(key, task, text)
            except Exception as exc:
                self.ctx.logger.warning("处理回复转达缓冲失败: %s", exc)

    # ------------------------------------------------------------------
    # Command：/私聊任务
    # ------------------------------------------------------------------

    @Command(
        "private_chat_tasks",
        description="查看或取消进行中的私聊传话任务",
        pattern=r"(?<!\S)/?私聊任务(?:\s+(?P<sub>取消))?(?:\s+(?P<target>\d+))?\s*$",
    )
    async def cmd_private_chat_tasks(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        """列出或取消当前聊天流发起的转达任务。"""

        stream_id = str(kwargs.get("stream_id") or "").strip()
        groups = kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"), dict) else {}
        sub = str(groups.get("sub") or "").strip()
        target = _normalize_qq_id(groups.get("target"))

        if sub == "取消":
            if not target:
                await self.ctx.send.text("用法：/私聊任务 取消 <对方QQ号>", stream_id)
                return True, "缺少目标 QQ 号", True
            removed = self._cancel_tasks(target_user_id=target, requester_stream_id=stream_id)
            if removed:
                await self.ctx.send.text(f"已取消 {removed} 个与 {target} 相关的传话任务。", stream_id)
            else:
                await self.ctx.send.text(f"没有找到与 {target} 相关的进行中传话任务。", stream_id)
            return True, "已处理取消请求", True

        lines: List[str] = []
        is_operator = bool(kwargs.get("is_local_operator"))
        for key, task in self._tasks.items():
            if not is_operator and task.requester_stream_id != stream_id:
                continue
            remaining = max(0, int(task.expire_at - time.time()) // 60)
            display = task.target_display_name or task.target_user_id
            lines.append(f"- 「{display}」（QQ:{task.target_user_id}），剩余等待约 {remaining} 分钟")
        if lines:
            await self.ctx.send.text("当前进行中的私聊传话任务：\n" + "\n".join(lines), stream_id)
        else:
            await self.ctx.send.text("当前没有进行中的私聊传话任务。", stream_id)
        return True, "已列出传话任务", True

    # ------------------------------------------------------------------
    # 授权与频率限制
    # ------------------------------------------------------------------

    def _is_authorized_requester(self, requester_user_id: str, group_id: str) -> bool:
        """判断发起传话请求的用户是否在授权范围内（白名单/黑名单模式）。

        白名单模式（默认）：私聊请求要求发起者在 ``users`` 名单中；群聊请求要求
        群在 ``groups`` 名单中，或发起者本人在 ``users`` 名单中（名单内的人在任何
        群都可用）。两份名单都为空时无人可用，需管理员先添加。
        黑名单模式：发起者命中 ``users`` 或群命中 ``groups`` 即拒绝，其余放行。
        """

        if not requester_user_id:
            # 拿不到发起者 QQ 的请求（如本地控制台消息）一律拒绝：
            # 无法匹配名单，也无法按用户限流与留日志追查
            return False

        security = self.config.security
        in_user_list = requester_user_id in security.users
        in_group_list = bool(group_id) and group_id in security.groups

        if security.mode == "blacklist":
            return not (in_user_list or in_group_list)
        return in_user_list or in_group_list

    def _check_rate_limit(self, requester_user_id: str, target_user_id: str) -> str:
        """检查并记录本次发送是否超出频率限制。

        Returns:
            str: 空字符串表示通过；否则返回给 Planner 阅读的拒绝原因。
        """

        security = self.config.security
        checks: List[Tuple[str, int, str]] = []
        if security.max_sends_per_user_per_hour > 0:
            checks.append((f"user:{requester_user_id}", security.max_sends_per_user_per_hour, "该用户"))
        if security.max_sends_per_target_per_hour > 0:
            checks.append((f"target:{target_user_id}", security.max_sends_per_target_per_hour, "该目标"))
        if security.max_sends_total_per_hour > 0:
            checks.append(("total", security.max_sends_total_per_hour, "全体"))

        now = time.time()
        for key, limit, label in checks:
            history = self._send_history.setdefault(key, deque())
            while history and now - history[0] > RATE_LIMIT_WINDOW_SECONDS:
                history.popleft()
            if len(history) >= limit:
                return f"发送频率已达上限（{label}每小时最多 {limit} 条），请稍后再试。"

        # 全部检查通过后才记录本次发送，避免部分计数
        for key, _, _ in checks:
            self._send_history.setdefault(key, deque()).append(now)
        return ""

    # ------------------------------------------------------------------
    # 目标解析
    # ------------------------------------------------------------------

    async def _resolve_target(self, target_id: str = "", target_name: str = "", group_id: str = "") -> Dict[str, Any]:
        """解析目标用户。

        Returns:
            dict: ``ok`` 为 True 时含 ``user_id``/``display_name``；
                  为 False 时含 ``error``，名字歧义时附带 ``candidates``。
        """

        normalized_id = _normalize_qq_id(target_id)
        if normalized_id:
            display = await self._lookup_display_name(normalized_id, group_id)
            return {"ok": True, "user_id": normalized_id, "display_name": display}

        name = str(target_name or "").strip()
        if not name:
            return {
                "ok": False,
                "error": "缺少目标用户：请提供 target_id（QQ 号）或 target_name（昵称/群名片/备注）。",
            }

        scored: Dict[str, Tuple[int, str]] = {}
        folded = name.casefold()

        def _collect(candidates: List[Dict[str, Any]]) -> None:
            for item in candidates:
                uid = _normalize_qq_id(item.get("user_id"))
                if not uid:
                    continue
                names = [
                    str(item.get(key) or "")
                    for key in ("card", "remark", "nickname", "user_nickname", "user_cardname")
                ]
                best = 0
                for candidate_name in names:
                    cleaned = candidate_name.strip()
                    if not cleaned:
                        continue
                    if cleaned.casefold() == folded:
                        best = max(best, 2)
                    elif folded in cleaned.casefold():
                        best = max(best, 1)
                if best == 0:
                    continue
                display = next((n.strip() for n in names if n.strip()), uid)
                if uid not in scored or best > scored[uid][0]:
                    scored[uid] = (best, display)

        if self.config.send.use_friend_list:
            _collect(self._extract_adapter_list(await self._call_adapter_api(API_FRIEND_LIST)))
        if group_id and self.config.send.use_group_members:
            _collect(
                self._extract_adapter_list(
                    await self._call_adapter_api(API_GROUP_MEMBER_LIST, {"group_id": int(group_id)})
                )
            )
        # 已有私聊流中的昵称也参与匹配
        try:
            streams = await self.ctx.chat.get_private_streams(platform=self.config.send.platform)
            if isinstance(streams, list):
                _collect([s for s in streams if isinstance(s, dict)])
        except Exception as exc:
            self.ctx.logger.debug("查询私聊流失败（忽略）: %s", exc)

        if not scored:
            return {
                "ok": False,
                "error": (
                    f"没有在好友列表、当前群成员或已有会话中找到叫「{name}」的联系人。"
                    "请让用户提供对方的 QQ 号后再试。"
                ),
            }

        best_score = max(score for score, _ in scored.values())
        hits = {uid: item for uid, (score, item) in scored.items() if score == best_score}
        if len(hits) == 1:
            uid, display = next(iter(hits.items()))
            return {"ok": True, "user_id": uid, "display_name": display}

        candidates = [
            {"user_id": uid, "display_name": display}
            for uid, display in list(hits.items())[:MAX_CANDIDATES]
        ]
        listing = "\n".join(
            f"{index}. {item['display_name']}（QQ:{item['user_id']}）"
            for index, item in enumerate(candidates, start=1)
        )
        return {
            "ok": False,
            "error": f"「{name}」匹配到多个联系人，请先向当前用户确认要联系哪一位：\n{listing}",
            "candidates": candidates,
        }

    async def _lookup_display_name(self, user_id: str, group_id: str = "") -> str:
        """尽力查询目标用户的展示名（备注 > 群名片 > 昵称 > 留空）。"""

        if self.config.send.use_friend_list:
            for item in self._extract_adapter_list(await self._call_adapter_api(API_FRIEND_LIST)):
                if _normalize_qq_id(item.get("user_id")) == user_id:
                    remark = str(item.get("remark") or "").strip()
                    nickname = str(item.get("nickname") or "").strip()
                    if remark or nickname:
                        return remark or nickname

        if group_id:
            resp = await self._call_adapter_api(
                API_GROUP_MEMBER_INFO, {"group_id": int(group_id), "user_id": int(user_id)}
            )
            if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
                data = resp["data"]
                card = str(data.get("card") or "").strip()
                nickname = str(data.get("nickname") or "").strip()
                if card or nickname:
                    return card or nickname

        stream = await self._find_existing_private_stream(user_id, self.config.send.platform)
        if stream and str(stream.get("user_nickname") or "").strip():
            return str(stream.get("user_nickname")).strip()

        resp = await self._call_adapter_api(API_STRANGER_INFO, {"user_id": int(user_id)})
        if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
            nickname = str(resp["data"].get("nickname") or "").strip()
            if nickname:
                return nickname
        return ""

    async def _resolve_requester_name(self, requester_user_id: str, group_id: str = "") -> str:
        """尽力查询请求者的展示名（群名片优先，其次私聊流昵称）。"""

        requester_user_id = str(requester_user_id or "").strip()
        if not requester_user_id:
            return ""
        if group_id:
            resp = await self._call_adapter_api(
                API_GROUP_MEMBER_INFO, {"group_id": int(group_id), "user_id": int(requester_user_id)}
            )
            if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
                card = str(resp["data"].get("card") or "").strip()
                nickname = str(resp["data"].get("nickname") or "").strip()
                if card or nickname:
                    return card or nickname
        stream = await self._find_existing_private_stream(requester_user_id, self.config.send.platform)
        if stream and str(stream.get("user_nickname") or "").strip():
            return str(stream.get("user_nickname")).strip()
        return ""

    # ------------------------------------------------------------------
    # 适配器 API 与路由身份
    # ------------------------------------------------------------------

    async def _call_adapter_api(self, api_name: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """调用 Napcat 适配器公开 API；适配器不可用时返回 None。"""

        try:
            resp = await self.ctx.api.call(api_name, params=params or {})
        except Exception as exc:
            self.ctx.logger.debug("调用适配器 API %s 失败: %s", api_name, exc)
            return None
        if isinstance(resp, dict) and resp.get("success") is False:
            self.ctx.logger.debug("适配器 API %s 返回失败: %s", api_name, resp.get("error"))
            return None
        return resp

    @staticmethod
    def _extract_adapter_list(resp: Any) -> List[Dict[str, Any]]:
        """从适配器 API 响应中提取 data 列表。"""

        if not isinstance(resp, dict):
            return []
        data = resp.get("data")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def _get_bot_account_id(self) -> str:
        """获取当前适配器登录账号 ID（带缓存）。"""

        if self._bot_account_id:
            return self._bot_account_id
        resp = await self._call_adapter_api(API_LOGIN_INFO)
        if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
            self._bot_account_id = _normalize_qq_id(resp["data"].get("user_id")) or str(
                resp["data"].get("user_id") or ""
            ).strip()
        return self._bot_account_id

    async def _get_adapter_route_identity(self) -> Tuple[str, str]:
        """获取适配器路由身份（account_id, scope），带缓存。

        与适配器 open_private_chat 工具的行为保持一致：
        account_id 取登录账号，scope 取适配器配置的 connection_id（默认空）。
        """

        account_id, scope_id, cached_at = self._route_cache
        if time.time() - cached_at < ROUTE_CACHE_SECONDS and (account_id or scope_id):
            return account_id, scope_id

        account_id = await self._get_bot_account_id()
        scope_id = ""
        try:
            adapter_config = await self.ctx.config.get_plugin(ADAPTER_PLUGIN_ID)
            if isinstance(adapter_config, dict):
                section = adapter_config.get(ADAPTER_SCOPE_SECTION)
                if isinstance(section, dict):
                    scope_id = str(section.get(ADAPTER_SCOPE_FIELD) or "").strip()
        except Exception as exc:
            self.ctx.logger.debug("读取适配器配置失败（scope 将使用空值）: %s", exc)

        if account_id or scope_id:
            self._route_cache = (account_id, scope_id, time.time())
        return account_id, scope_id

    async def _find_existing_private_stream(self, user_id: str, platform: str) -> Optional[Dict[str, Any]]:
        """按 QQ 号查找已存在的私聊流。"""

        try:
            stream = await self.ctx.chat.get_stream_by_user_id(user_id, platform=platform)
        except Exception:
            return None
        if isinstance(stream, dict) and str(stream.get("session_id") or "").strip():
            return stream
        return None

    async def _try_enable_adapter_tool(self) -> None:
        """尝试启用适配器的 open_private_chat 工具（默认关闭，需在配置中显式开启）。

        该操作会改变其他插件的安全相关组件状态：适配器的 open_private_chat 工具
        发送首条消息时会授予 15 分钟的私聊名单临时放行。因此仅在用户明确开启
        ``security.enable_adapter_open_tool`` 时才执行，且为尽力而为（适配器后续
        重载配置时可能再次覆盖组件状态）。
        """

        if not self.config.security.enable_adapter_open_tool:
            return
        try:
            result = await self.ctx.component.enable_component(f"{ADAPTER_PLUGIN_ID}.open_private_chat", "tool")
            if isinstance(result, dict) and result.get("success") is False:
                self.ctx.logger.info(
                    "未能启用适配器 open_private_chat 工具: %s（不影响本插件发送私聊）",
                    result.get("error"),
                )
            else:
                self.ctx.logger.info(
                    "已按配置启用适配器 open_private_chat 工具（该工具首条消息可建立 15 分钟临时私聊放行）"
                )
        except Exception as exc:
            self.ctx.logger.info("启用适配器 open_private_chat 工具失败（不影响本插件发送私聊）: %s", exc)

    # ------------------------------------------------------------------
    # 内容润色（可选）
    # ------------------------------------------------------------------

    async def _polish_content(self, text: str, requester_name: str) -> str:
        """用 replyer 模型润色转达内容；失败时保留原文。"""

        prompt = (
            "你在帮用户润色一条即将通过 QQ 私聊转达出去的消息。"
            "请原样保留消息的全部信息和意图，只把措辞调整得更自然、更像真人随手打出来的话；"
            "不要添加新信息，不要改变语气倾向，直接输出润色后的消息全文，不要任何解释或引号。\n"
            f"转达人：{requester_name or '用户'}\n"
            f"原始消息：{text}"
        )
        try:
            resp = await self.ctx.llm.generate(prompt, model="replyer")
        except Exception as exc:
            self.ctx.logger.warning("润色转达内容失败，保留原文: %s", exc)
            return text
        if isinstance(resp, dict) and resp.get("success") and str(resp.get("response") or "").strip():
            return str(resp["response"]).strip()
        return text

    # ------------------------------------------------------------------
    # 转达任务管理
    # ------------------------------------------------------------------

    def _task_key(self, requester_stream_id: str, target_user_id: str) -> str:
        """任务键：请求者所在聊天流 + 目标用户。"""

        return f"{requester_stream_id}|{target_user_id}"

    def _register_task(
        self,
        *,
        target_user_id: str,
        target_display_name: str,
        target_stream_id: str,
        requester_user_id: str,
        requester_stream_id: str,
    ) -> _RelayTask:
        """注册或刷新一条转达任务。"""

        key = self._task_key(requester_stream_id, target_user_id)
        now = time.time()
        expire_at = now + self.config.relay.reply_timeout_minutes * 60
        existing = self._tasks.get(key)
        if existing is not None:
            existing.target_display_name = target_display_name or existing.target_display_name
            existing.target_stream_id = target_stream_id or existing.target_stream_id
            existing.expire_at = max(existing.expire_at, expire_at)
            task = existing
        else:
            task = _RelayTask(
                target_user_id=target_user_id,
                target_display_name=target_display_name,
                target_stream_id=target_stream_id,
                requester_user_id=requester_user_id,
                requester_stream_id=requester_stream_id,
                created_at=now,
                expire_at=expire_at,
            )
            self._tasks[key] = task
        self._save_tasks()
        self._ensure_watchdog(key)
        return task

    def _cancel_tasks(self, target_user_id: str, requester_stream_id: str) -> int:
        """取消指定目标（可选限定请求者流）的任务，返回取消数量。"""

        removed = 0
        for key, task in list(self._tasks.items()):
            if task.target_user_id != target_user_id:
                continue
            if requester_stream_id and task.requester_stream_id != requester_stream_id:
                continue
            self._remove_task(key)
            removed += 1
        return removed

    def _remove_task(self, key: str) -> None:
        """移除任务并清理其后台协程。"""

        task = self._tasks.pop(key, None)
        if task is not None and task.flush_delay_task is not None and not task.flush_delay_task.done():
            task.flush_delay_task.cancel()
        watchdog = self._watchdogs.pop(key, None)
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()
        self._save_tasks()

    def _ensure_watchdog(self, key: str) -> None:
        """确保任务有超时看门狗在运行。"""

        existing = self._watchdogs.get(key)
        if existing is not None and not existing.done():
            return
        self._watchdogs[key] = asyncio.create_task(self._task_watchdog(key))

    async def _task_watchdog(self, key: str) -> None:
        """看门狗：任务到期后通知请求者并移除任务。"""

        while True:
            task = self._tasks.get(key)
            if task is None:
                return
            now = time.time()
            if now >= task.expire_at:
                self._tasks.pop(key, None)
                self._watchdogs.pop(key, None)
                self._save_tasks()
                if self.config.relay.notify_on_timeout:
                    await self._notify_timeout(task)
                return
            await asyncio.sleep(min(30.0, max(1.0, task.expire_at - now)))

    async def _notify_timeout(self, task: _RelayTask) -> None:
        """任务超时后给请求者发自然语言提示。"""

        display = task.target_display_name or task.target_user_id
        minutes = self.config.relay.reply_timeout_minutes
        hint = ""
        if self.config.relay.hint_filter_issue:
            hint = (
                "另外，如果对方其实回复了但你一直没收到消息，"
                "可能是 Napcat 适配器的私聊名单过滤把对方消息拦截了："
                "可以在适配器配置中关闭「启用聊天名单过滤」，或把对方 QQ 加入私聊白名单。"
            )
        intent = (
            f"你之前帮当前用户把一条消息转达给了「{display}」（QQ:{task.target_user_id}），"
            f"但等待 {minutes} 分钟后对方一直没有回复。请自然地把这件事告诉当前用户。{hint}"
        )
        fallback = f"⏳ 「{display}」一直没有回复，这条传话我先收起来了。{hint}"
        await self._notify_stream(task.requester_stream_id, intent, fallback)

    # ------------------------------------------------------------------
    # 回复合并与转达
    # ------------------------------------------------------------------

    def _buffer_reply(self, key: str, task: _RelayTask, text: str) -> None:
        """把对方的新回复放入合并缓冲，并在静默期后触发转达。"""

        task.buffer.append(text)
        # 回复活跃时滚动延长等待窗口，但不超过任务最长存活时间
        now = time.time()
        rolling = now + self.config.relay.reply_timeout_minutes * 60
        cap = task.created_at + self.config.relay.max_task_lifetime_minutes * 60
        task.expire_at = min(max(task.expire_at, rolling), cap)
        self._save_tasks()
        self._ensure_watchdog(key)

        if task.flush_delay_task is not None and not task.flush_delay_task.done():
            task.flush_delay_task.cancel()
        task.flush_delay_task = asyncio.create_task(self._flush_delayed(key))

    async def _flush_delayed(self, key: str) -> None:
        """合并静默期结束后转达缓冲中的回复。"""

        await asyncio.sleep(self.config.relay.aggregate_seconds)
        task = self._tasks.get(key)
        if task is None or not task.buffer:
            return
        texts = task.buffer[:]
        task.buffer.clear()
        self._save_tasks()

        joined = "\n".join(text for text in texts if text)
        display = task.target_display_name or task.target_user_id
        intent = (
            f"你之前帮当前用户把一条消息转达给了「{display}」（QQ:{task.target_user_id}），"
            f"对方刚刚回复了：「{joined}」。"
            "请把这条回复自然地告诉当前用户，可以加上你自己的反应。"
        )
        fallback = f"📩 「{display}」回复了你：\n{joined}"
        await self._notify_stream(task.requester_stream_id, intent, fallback)

    async def _notify_stream(self, stream_id: str, intent: str, fallback_text: str) -> None:
        """向请求者所在聊天流转达消息：优先 Maisaka 主动回合，失败回退纯文本。"""

        stream_id = str(stream_id or "").strip()
        if not stream_id:
            self.ctx.logger.warning("转达目标聊天流为空，放弃转达: %s", intent[:50])
            return

        if self.config.relay.notify_via_planner:
            try:
                resp = await self.ctx.maisaka.proactive.trigger(
                    stream_id,
                    intent,
                    reason="private_chat_tool_plus:relay",
                )
                if isinstance(resp, dict) and resp.get("success") is False:
                    self.ctx.logger.warning("Maisaka 主动回合触发失败，回退纯文本: %s", resp.get("error"))
                else:
                    return
            except Exception as exc:
                self.ctx.logger.warning("Maisaka 主动回合触发异常，回退纯文本: %s", exc)

        try:
            await self.ctx.send.text(fallback_text, stream_id)
        except Exception as exc:
            self.ctx.logger.error("转达消息发送失败: %s", exc)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _tasks_file_path(self) -> Any:
        """状态文件路径。"""

        return self.ctx.paths.data_dir / TASKS_FILE_NAME

    def _save_tasks(self) -> None:
        """把任务状态持久化到 data 目录。"""

        try:
            path = self._tasks_file_path()
            if not self._tasks:
                # 没有进行中的任务时直接删除状态文件
                path.unlink(missing_ok=True)
                return
            payload = {key: task.to_dict() for key, task in self._tasks.items()}
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.ctx.logger.warning("保存转达任务状态失败: %s", exc)

    def _restore_tasks(self) -> None:
        """从 data 目录恢复任务状态，并重新拉起看门狗。"""

        path = self._tasks_file_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.ctx.logger.warning("读取转达任务状态失败: %s", exc)
            return
        if not isinstance(payload, dict):
            return

        now = time.time()
        for key, item in payload.items():
            if not isinstance(item, dict):
                continue
            try:
                task = _RelayTask.from_dict(item)
            except Exception as exc:
                self.ctx.logger.warning("恢复转达任务 %s 失败: %s", key, exc)
                continue
            if now >= task.expire_at or not task.target_user_id or not task.requester_stream_id:
                continue
            self._tasks[str(key)] = task
            # 重启前尚未转达的缓冲回复，恢复后立即安排一次合并转达
            if task.buffer:
                task.flush_delay_task = asyncio.create_task(self._flush_delayed(str(key)))
            self._ensure_watchdog(str(key))


def create_plugin() -> PrivateChatToolPlusPlugin:
    """Runner 加载入口。"""

    return PrivateChatToolPlusPlugin()
