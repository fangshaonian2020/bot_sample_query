# main.py
# 少数胜（A/B）回合制游戏插件（修复：不再使用 is_group/is_private，改为通过 get_group_id 判断）

from __future__ import annotations
from typing import Dict, Set, Optional, List, Tuple
from dataclasses import dataclass, field
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# --------- 工具：根据事件判断群/私聊 ---------
def evt_group_id(event: AstrMessageEvent) -> Optional[int]:
    try:
        gid = event.get_group_id()
        return gid
    except Exception:
        return None

def is_group_event(event: AstrMessageEvent) -> bool:
    gid = evt_group_id(event)
    return gid is not None and gid != 0

def is_private_event(event: AstrMessageEvent) -> bool:
    return not is_group_event(event)

# --------- 状态 ---------
@dataclass
class GameState:
    group_id: Optional[int] = None
    title: str = "少数胜游戏"
    registered: Set[int] = field(default_factory=set)
    running: bool = False
    round_index: int = 0  # 从1开始
    total_rounds: int = 5
    in_round: bool = False
    choices: Dict[int, str] = field(default_factory=dict)  # user_id -> "A"/"B"
    scores: Dict[int, int] = field(default_factory=dict)   # user_id -> score
    overtime: bool = False  # 是否处于延长赛模式

@register("minor_game", "YourName", "少数胜 A/B 回合制游戏", "1.0.1")
class MinorGame(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.state = GameState()

    async def initialize(self):
        logger.info("[minor_game] 插件已加载")

    async def terminate(self):
        logger.info("[minor_game] 插件已卸载")

    # 发送群文本（不同版本的 bot API 名称可能不同，这里做多重兼容）
    async def send_group(self, group_id: int, text: str):
        bot = getattr(self.context, "bot", None)
        # 常见方法名：send_group_message / send_group_msg
        for name in ("send_group_message", "send_group_msg"):
            func = getattr(bot, name, None)
            if callable(func):
                return await func(group_id, text)
        # 兜底：call_api
        call_api = getattr(bot, "call_api", None)
        if callable(call_api):
            return await call_api("send_group_msg", group_id=group_id, message=text)
        raise AttributeError("Bot 不支持发送群消息的已知方法，请告知我实际 API 名称。")

    # 发送私聊
    async def send_private(self, user_id: int, text: str):
        bot = getattr(self.context, "bot", None)
        for name in ("send_private_message", "send_private_msg"):
            func = getattr(bot, name, None)
            if callable(func):
                return await func(user_id, text)
        call_api = getattr(bot, "call_api", None)
        if callable(call_api):
            return await call_api("send_private_msg", user_id=user_id, message=text)
        raise AttributeError("Bot 不支持发送私聊消息的已知方法，请告知我实际 API 名称。")

    # 1) 管理：宣布活动并指定群
    @filter.command("announce_game")
    async def announce_game(self, event: AstrMessageEvent):
        """
        /announce_game <group_id> <title...>
        在指定群宣布活动、清空旧状态，等待玩家 /register
        """
        args = (event.message_str or "").strip().split(maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法：/announce_game <群号> <标题，可选>")
            return

        try:
            gid = int(args[0])
        except Exception:
            yield event.plain_result("群号必须是数字：/announce_game <群号> <标题>")
            return

        title = args[1] if len(args) > 1 else "少数胜游戏"
        self.state = GameState(group_id=gid, title=title)
        await self.send_group(gid, f"【{title}】\n活动开始报名！请在本群发送 /register 报名参加。管理员可用 /start_game 开始游戏。")
        yield event.plain_result("已发布活动并重置状态。")

    # 2) 玩家报名（仅在目标群）
    @filter.command("register")
    async def register(self, event: AstrMessageEvent):
        """
        /register
        仅在活动群中有效。报名成功后系统会维护你的分数。
        """
        s = self.state
        if not s.group_id:
            yield event.plain_result("当前没有正在报名的活动。请等待管理员 /announce_game。")
            return

        if not is_group_event(event) or evt_group_id(event) != s.group_id:
            yield event.plain_result(f"请到目标群 {s.group_id} 内发送 /register 报名。")
            return

        uid = event.get_sender_id()
        s.registered.add(uid)
        s.scores.setdefault(uid, 0)
        yield event.plain_result("报名成功！等待管理员 /start_game。")

    # 2) 管理：开始游戏
    @filter.command("start_game")
    async def start_game(self, event: AstrMessageEvent):
        """
        /start_game [轮数]
        开始游戏，默认5轮
        """
        s = self.state
        if not s.group_id:
            yield event.plain_result("尚未发布活动。先用 /announce_game <群号> <标题>。")
            return
        if s.running:
            yield event.plain_result("游戏已在进行中。")
            return
        if len(s.registered) < 1:
            yield event.plain_result("还没有报名的玩家。")
            return

        parts = (event.message_str or "").strip().split()
        if len(parts) >= 1 and parts[0].isdigit():
            s.total_rounds = int(parts[0])

        s.running = True
        s.round_index = 0
        s.overtime = False
        await self.send_group(s.group_id, f"【{s.title}】开始！本局共 {s.total_rounds} 轮。报名人数：{len(s.registered)}。")
        await self._start_next_round()

    # 3) 启动下一轮
    async def _start_next_round(self):
        s = self.state
        s.round_index += 1
        s.in_round = True
        s.choices.clear()

        round_type = "延长赛" if s.overtime else f"第{s.round_index}轮"
        prompt = (
            f"{round_type}开始！\n"
            "规则：请私聊我发送 /A 或 /B 进行选择（大小写均可）。\n"
            "少数方胜；若 A/B 持平，则奇数轮 A 胜，偶数轮 B 胜。\n"
            "管理员可 /end_round 结算本轮。"
        )
        await self.send_group(s.group_id, prompt)

        # 可选：私聊提醒
        for uid in s.registered:
            try:
                await self.send_private(uid, f"[{s.title}] {round_type} 已开始，请私聊发送 /A 或 /B。可重复修改，以最后一次为准。")
            except Exception as e:
                logger.debug(f"私聊提醒失败 uid={uid}: {e}")

    # 3) 玩家私聊提交 A/B（支持大小写）
    @filter.command("A")
    async def choose_A(self, event: AstrMessageEvent):
        await self._handle_choice(event, "A")

    @filter.command("a")
    async def choose_a(self, event: AstrMessageEvent):
        await self._handle_choice(event, "A")

    @filter.command("B")
    async def choose_B(self, event: AstrMessageEvent):
        await self._handle_choice(event, "B")

    @filter.command("b")
    async def choose_b(self, event: AstrMessageEvent):
        await self._handle_choice(event, "B")

    async def _handle_choice(self, event: AstrMessageEvent, choice: str):
        s = self.state
        if not is_private_event(event):
            # 只允许私聊提交
            return

        if not s.running or not s.in_round:
            yield event.plain_result("当前不在提交阶段。")
            return

        uid = event.get_sender_id()
        if uid not in s.registered:
            yield event.plain_result("你尚未报名。请在活动群内发送 /register 报名。")
            return

        s.choices[uid] = choice
        yield event.plain_result(f"已记录你的选择：{choice}（可重复修改，以最后一次为准）")

    # 4) 管理：结束当前轮并结算
    @filter.command("end_round")
    async def end_round(self, event: AstrMessageEvent):
        s = self.state
        if not s.running or not s.in_round:
            yield event.plain_result("当前没有进行中的轮次。")
            return

        await self._settle_round()

        # 5) 进入下一轮或结束
        if not s.overtime and s.round_index >= s.total_rounds:
            # 正常轮打完，检查是否需要延长赛
            leaders, top = self._leaders()
            if len(leaders) >= 2:
                s.overtime = True
                await self.send_group(s.group_id, f"前 {s.total_rounds} 轮结束，最高分并列（{top} 分），进入延长赛！")
                await self._start_next_round()
            else:
                await self._finish_game()
        elif s.overtime:
            # 延长赛：若仍平分，继续；否则结束
            leaders, _ = self._leaders()
            if len(leaders) >= 2:
                await self._start_next_round()
            else:
                await self._finish_game()
        else:
            await self._start_next_round()

    # 6) 管理：强制结束游戏
    @filter.command("end_game")
    async def end_game(self, event: AstrMessageEvent):
        s = self.state
        if not s.running:
            yield event.plain_result("没有进行中的游戏。")
            return
        if s.in_round:
            await self._settle_round()
        await self._finish_game()

    # 结算当前轮
    async def _settle_round(self):
        s = self.state
        s.in_round = False
        a = sum(1 for v in s.choices.values() if v == "A")
        b = sum(1 for v in s.choices.values() if v == "B")
        round_type = "延长赛" if s.overtime else f"第{s.round_index}轮"

        # 判定胜负
        if a == b:
            winner = "A" if s.round_index % 2 == 1 else "B"
            reason = f"人数相等，按轮次奇偶判定：{winner} 胜"
        elif a < b:
            winner, reason = "A", "少数方胜"
        else:
            winner, reason = "B", "少数方胜"

        # 给获胜方玩家加分
        winners: List[int] = [uid for uid, c in s.choices.items() if c == winner]
        for uid in winners:
            s.scores[uid] = s.scores.get(uid, 0) + 1

        # 公布本轮结果
        lines = [
            f"{round_type} 结算：",
            f"A 票数：{a} 人",
            f"B 票数：{b} 人",
            f"胜方：{winner}（{reason}）",
            f"本轮加分：胜方玩家 +1 分",
        ]
        await self.send_group(s.group_id, "\n".join(lines))

    # 结束游戏，公布总分
    async def _finish_game(self):
        s = self.state
        s.running = False
        s.in_round = False

        # 排行
        ranking = sorted(s.scores.items(), key=lambda kv: (-kv[1], kv[0]))
        if not ranking:
            await self.send_group(s.group_id, "本次游戏无人得分。")
        else:
            lines = [f"【{s.title}】最终结果"]
            for i, (uid, sc) in enumerate(ranking, 1):
                lines.append(f"{i}. 玩家{uid}：{sc} 分")
            await self.send_group(s.group_id, "\n".join(lines))

        # 清理本局状态但保留 group_id/title 以便复用
        gid, title = s.group_id, s.title
        self.state = GameState(group_id=gid, title=title)

    # 计算领先者
    def _leaders(self) -> Tuple[List[int], int]:
        s = self.state
        if not s.scores:
            return [], 0
        top = max(s.scores.values())
        leaders = [uid for uid, sc in s.scores.items() if sc == top]
        return leaders, top
