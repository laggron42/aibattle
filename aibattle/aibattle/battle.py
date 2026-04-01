from __future__ import annotations

import asyncio
import enum
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord.ui import ActionRow, Button, Section, TextDisplay, TextInput, Thumbnail
from discord.utils import format_dt
from mistralai.client import Mistral
from mistralai.client.models import MessageOutputEntry, TextChunk

from ballsdex.core.discord import UNKNOWN_INTERACTION, Container, LayoutView, Modal
from ballsdex.core.utils.buttons import ConfirmChoiceView
from bd_models.models import BallInstance, Player
from settings.models import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

    from .cog import AIBattle

type Interaction = discord.Interaction[BallsDexBot]

log = logging.getLogger("ballsdex.packages.aibattle")

MISTRAL_API_KEY = "REPLACE ME"
MISTRAL_AGENT_ID = "REPLACE ME"
BATTLE_TIMEOUT = 60 * 30  # 30 minutes
PROMPT_TIMEOUT = 60 * 5  # 5 minutes per prompt phase

# Dedicated executor for Mistral API calls to avoid starving the default executor
# used by Django ORM (SyncToAsync) and other async operations.
_mistral_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mistral")


class BattlePhase(enum.Enum):
    SETUP = "setup"
    PROMPTING = "prompting"
    THINKING = "thinking"
    RESULT = "result"
    FINISHED = "finished"


class BattleResult(enum.Enum):
    PLAYER1_WON = "PLAYER 1 WON"
    PLAYER2_WON = "PLAYER 2 WON"
    ENDED = "ENDED"
    NEXT_TURN = "NEXT TURN"


class PromptModal(Modal, title="Enter your battle prompt"):
    prompt = TextInput(
        label="Your prompt (200 chars max)",
        style=discord.TextStyle.paragraph,
        max_length=200,
        placeholder="Describe your battle strategy...",
    )

    def __init__(self, battle_user: BattleUser):
        super().__init__()
        self.battle_user = battle_user

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.battle_user.user.id:
            await interaction.response.send_message("This is not your prompt.", ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    async def on_submit(self, interaction: Interaction):
        battle = self.battle_user.battle
        if battle.phase != BattlePhase.PROMPTING:
            await interaction.response.send_message("Cannot submit a prompt right now.", ephemeral=True)
            return
        if self.battle_user.prompt_submitted:
            await interaction.response.send_message("You have already submitted a prompt this turn.", ephemeral=True)
            return

        self.battle_user.current_prompt = self.prompt.value.strip()
        self.battle_user.prompt_submitted = True
        await interaction.response.defer()

        if battle.user1.prompt_submitted and battle.user2.prompt_submitted:
            battle.phase = BattlePhase.THINKING
            await battle.edit_message(interaction)
            await battle.call_ai()
        else:
            await battle.edit_message(interaction)


class BattleUser(Container):
    """
    Represents one user participating in a battle.

    Attributes
    ----------
    user: discord.abc.User
        The Discord user.
    player: Player
        The database player model.
    proposal: list[BallInstance]
        The list of countryballs selected for battle.
    locked: bool
        Whether the proposal is locked.
    cancelled: bool
        Whether this user cancelled the battle.
    prompt_submitted: bool
        Whether the user submitted a prompt this turn.
    current_prompt: str | None
        The user's prompt for the current turn.
    """

    def __init__(self, user: discord.abc.User, player: Player):
        super().__init__()
        self.user = user
        self.player = player
        self.battle: BattleInstance
        self.proposal: list[BallInstance] = []
        self.locked: bool = False
        self.cancelled: bool = False
        self.prompt_submitted: bool = False
        self.current_prompt: str | None = None

    def __repr__(self) -> str:
        return f"<BattleUser player_id={self.player.pk} discord_id={self.user.id}>"

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id not in (self.battle.user1.user.id, self.battle.user2.user.id):
            await interaction.response.send_message("You are not part of this battle!", ephemeral=True)
            return False
        return True

    def _proposal_text(self) -> str:
        if not self.proposal:
            return "No countryballs selected yet."
        lines: list[str] = []
        for ball in self.proposal:
            lines.append(f"- {ball.description(include_emoji=True, bot=self.battle.cog.bot, is_trade=True)}")
        return "\n".join(lines)

    async def refresh_container(self):
        """Rebuild this container's items based on the current battle phase."""
        self.clear_items()

        phase = self.battle.phase
        section = Section(
            TextDisplay(f"## {self.user.display_name}"), accessory=Thumbnail(self.user.display_avatar.url)
        )

        if self.battle.cancelled:
            if self.cancelled:
                self.accent_colour = discord.Colour.red()
                section.add_item(TextDisplay("You have cancelled the battle."))
            else:
                self.accent_colour = discord.Colour.red()
                section.add_item(TextDisplay("The battle has been cancelled."))
            self.add_item(section)
        elif phase == BattlePhase.SETUP:
            if self.locked:
                self.accent_colour = discord.Colour.yellow()
                section.add_item(TextDisplay("Proposal locked. Waiting for the other player to lock theirs."))
            else:
                self.accent_colour = discord.Colour.blue()
                section.add_item(
                    TextDisplay(
                        f"Add {self.battle.amount} {settings.plural_collectible_name} "
                        f"to your team, then lock your proposal."
                    )
                )
            section.add_item(
                TextDisplay(f"-# {len(self.proposal)}/{self.battle.amount} {settings.plural_collectible_name} selected")
            )
            self.add_item(section)
            self.add_item(TextDisplay(self._proposal_text()))
        elif phase == BattlePhase.PROMPTING:
            if self.prompt_submitted:
                self.accent_colour = discord.Colour.green()
                section.add_item(TextDisplay("Prompt submitted! Waiting for the other player..."))
            else:
                self.accent_colour = discord.Colour.blue()
                section.add_item(TextDisplay("Click the button below to enter your prompt."))
            self.add_item(section)
            if self.battle.turn == 1:
                self.add_item(TextDisplay(self._proposal_text()))
        elif phase == BattlePhase.THINKING:
            self.accent_colour = discord.Colour.gold()
            section.add_item(TextDisplay(f"**Prompt:** {self.current_prompt}"))
            self.add_item(section)
        elif phase in (BattlePhase.RESULT, BattlePhase.FINISHED):
            if phase == BattlePhase.FINISHED:
                self.accent_colour = discord.Colour.dark_grey()
            else:
                self.accent_colour = discord.Colour.green()
            section.add_item(TextDisplay(f"**Prompt:** {self.current_prompt}"))
            self.add_item(section)
        else:
            self.add_item(section)

        if phase == BattlePhase.SETUP:
            return  # already added items above

        if not self.battle.active:
            for item in self.walk_children():
                if hasattr(item, "disabled"):
                    item.disabled = True  # type: ignore

    async def lock(self):
        """
        Lock the proposal.

        Raises
        ------
        RuntimeError
            If already locked, cancelled, or wrong number of countryballs.
        """
        if self.locked:
            raise RuntimeError("You have already locked your proposal.")
        if self.battle.cancelled:
            raise RuntimeError("The battle has been cancelled.")
        if len(self.proposal) != self.battle.amount:
            raise RuntimeError(
                f"You need exactly {self.battle.amount} {settings.plural_collectible_name} "
                f"in your proposal. You currently have {len(self.proposal)}."
            )
        self.locked = True

    async def cancel(self):
        """Cancel the battle."""
        self.cancelled = True
        self.battle.stop()
        await self.battle.cleanup()

    def reset_prompt(self):
        """Clear prompt state for a new turn."""
        self.prompt_submitted = False
        self.current_prompt = None


class BattleInstance(LayoutView):
    """
    The main battle view managing the entire AI battle flow.

    Attributes
    ----------
    user1: BattleUser
        The first player.
    user2: BattleUser
        The second player.
    phase: BattlePhase
        The current phase of the battle.
    turn: int
        The current turn number.
    """

    def __init__(
        self,
        cog: "AIBattle",
        interaction: discord.Interaction,
        user1: BattleUser,
        user2: BattleUser,
        *,
        duplicates: bool = False,
        amount: int = 5,
    ):
        super().__init__(timeout=BATTLE_TIMEOUT)
        self.cog = cog
        self.original_interaction = interaction
        self.user1 = user1
        self.user2 = user2
        self.duplicates = duplicates
        self.amount = amount

        self.user1.battle = self
        self.user2.battle = self

        self.message: discord.Message
        self.phase = BattlePhase.SETUP
        self.turn: int = 1
        self.ai_response: str | None = None
        self.conversation_id: str | None = None
        self.result: BattleResult | None = None
        self.battle_log: list[dict[str, str]] = []

        self.edit_lock = asyncio.Lock()
        self.next_edit_interaction: Interaction | None = None
        self.timeout_task = asyncio.create_task(self._timeout(), name=f"battle-timeout-{id(self)}")
        self.prompt_timeout_task: asyncio.Task | None = None

        self.client = Mistral(api_key=MISTRAL_API_KEY)

    @property
    def current_view(self) -> BattleInstance:
        return self

    @property
    def cancelled(self) -> bool:
        return self.user1.cancelled or self.user2.cancelled

    @property
    def active(self) -> bool:
        return not self.is_finished() and not self.cancelled

    def _get_user(self, user: discord.User | discord.Member) -> BattleUser:
        if user.id == self.user1.user.id:
            return self.user1
        if user.id == self.user2.user.id:
            return self.user2
        raise RuntimeError("User is not part of this battle.")

    async def on_error(self, interaction: Interaction, error: Exception, item: discord.ui.Item) -> None:
        if isinstance(error, discord.NotFound) and error.code in UNKNOWN_INTERACTION:
            log.warning("Expired interaction", exc_info=error)
            return
        log.exception(f"Error in battle between {self.user1} and {self.user2}", exc_info=error)
        await self.cleanup()
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("An error occurred, the battle will be cancelled.", ephemeral=True)
        self.phase = BattlePhase.FINISHED
        await self._rebuild_and_edit(None)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id not in (self.user1.user.id, self.user2.user.id):
            await interaction.response.send_message("You are not part of this battle!", ephemeral=True)
            return False
        return True

    # ==== Buttons ====

    buttons = ActionRow()

    @buttons.button(label="Lock Proposal", emoji="\N{LOCK}", style=discord.ButtonStyle.primary)
    async def lock_button(self, interaction: Interaction, button: Button):
        user = self._get_user(interaction.user)
        if user.locked:
            await interaction.response.send_message("You have already locked your proposal!", ephemeral=True)
            return

        try:
            await user.lock()
        except RuntimeError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer()

        if self.user1.locked and self.user2.locked:
            self.phase = BattlePhase.PROMPTING
            self._start_prompt_timeout()

        await self.edit_message(interaction)

    @buttons.button(label="Enter Prompt", emoji="\N{PENCIL}", style=discord.ButtonStyle.primary)
    async def prompt_button(self, interaction: Interaction, button: Button):
        user = self._get_user(interaction.user)
        if self.phase != BattlePhase.PROMPTING:
            await interaction.response.send_message("You cannot submit a prompt right now.", ephemeral=True)
            return
        if user.prompt_submitted:
            await interaction.response.send_message("You have already submitted a prompt this turn.", ephemeral=True)
            return

        modal = PromptModal(user)
        await interaction.response.send_modal(modal)

    @buttons.button(
        label="Cancel", emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}", style=discord.ButtonStyle.danger
    )
    async def cancel_button(self, interaction: Interaction, button: Button):
        user = self._get_user(interaction.user)
        view = ConfirmChoiceView(
            interaction, accept_message="Cancelling the battle...", cancel_message="Cancellation aborted."
        )
        await interaction.response.send_message(
            "Are you sure you want to cancel this battle?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        await user.cancel()
        await self.edit_message(None)

    # ==== Start ====

    async def start(self):
        """Send the initial battle message."""
        self.clear_items()
        self.add_item(TextDisplay("# \N{CROSSED SWORDS} AI Battle mode"))
        self.add_item(
            TextDisplay(f"Hey {self.user2.user.mention}, {self.user1.user.mention} is challenging you to a battle!")
        )
        self.add_item(self.user1)
        self.add_item(self.user2)

        self.buttons.clear_items()
        self.buttons.add_item(self.lock_button)
        self.buttons.add_item(self.cancel_button)
        self.add_item(self.buttons)

        timeout = datetime.now() + timedelta(seconds=BATTLE_TIMEOUT)
        self.add_item(TextDisplay(f"-# This battle will timeout {format_dt(timeout, style='R')}."))

        await self.user1.refresh_container()
        await self.user2.refresh_container()

        self.message = await self.original_interaction.channel.send(view=self)

    # ==== Message editing (LIFO queue pattern) ====

    async def edit_message(self, interaction: Interaction | None):
        if interaction is not None:
            self.next_edit_interaction = interaction
        if self.edit_lock.locked():
            return
        async with self.edit_lock:
            if self.next_edit_interaction is None:
                await asyncio.sleep(0.5)
                await self._rebuild_and_edit(None)
                return
            while self.next_edit_interaction is not None:
                inter = self.next_edit_interaction
                self.next_edit_interaction = None
                await asyncio.sleep(0.5)
                await self._rebuild_view()
                if self.is_finished():
                    for child in self.walk_children():
                        if hasattr(child, "disabled"):
                            child.disabled = True  # type: ignore
                await inter.edit_original_response(view=self)
                if self.is_finished():
                    break

    async def _rebuild_and_edit(self, interaction: Interaction | None):
        """Rebuild the view and edit via the message object (no interaction)."""
        await self._rebuild_view()
        if self.is_finished():
            for child in self.walk_children():
                if hasattr(child, "disabled"):
                    child.disabled = True  # type: ignore
        await self.message.edit(view=self)

    async def _rebuild_view(self):
        """Rebuild all items in the view based on current state."""
        self.clear_items()

        # Header
        self.add_item(TextDisplay("# \N{CROSSED SWORDS} AI Battle mode"))
        if self.phase == BattlePhase.SETUP:
            header = f"Battle between {self.user1.user.display_name} and {self.user2.user.display_name}"
        else:
            header = (
                f"Battle between {self.user1.user.display_name} and {self.user2.user.display_name} - Turn {self.turn}"
            )
        self.add_item(TextDisplay(f"## {header}"))

        # During PROMPTING on turn 2+, show last turn's result ABOVE containers (chronological)
        if self.phase == BattlePhase.PROMPTING and self.ai_response:
            self.add_item(TextDisplay(f"### Last turn's result\n{self.ai_response}"))

        # User containers
        await self.user1.refresh_container()
        await self.user2.refresh_container()
        self.add_item(self.user1)
        self.add_item(self.user2)

        # AI response section (below containers for THINKING/RESULT/FINISHED)
        if self.phase == BattlePhase.THINKING:
            self.add_item(TextDisplay("### AI is thinking... \N{HOURGLASS WITH FLOWING SAND}"))
        elif self.phase in (BattlePhase.RESULT, BattlePhase.FINISHED) and self.ai_response:
            self.add_item(TextDisplay(f"### AI Response\n{self.ai_response}"))
            if self.result:
                if self.result == BattleResult.PLAYER1_WON:
                    self.add_item(TextDisplay(f"## \N{TROPHY} {self.user1.user.display_name} wins!"))
                elif self.result == BattleResult.PLAYER2_WON:
                    self.add_item(TextDisplay(f"## \N{TROPHY} {self.user2.user.display_name} wins!"))
                elif self.result == BattleResult.ENDED:
                    self.add_item(TextDisplay("## The battle has ended in a draw!"))
                elif self.result == BattleResult.NEXT_TURN:
                    self.add_item(TextDisplay(f"-# Preparing turn {self.turn}..."))

        # Buttons
        self.buttons.clear_items()
        if self.phase == BattlePhase.SETUP:
            self.buttons.add_item(self.lock_button)
            self.buttons.add_item(self.cancel_button)
        elif self.phase == BattlePhase.PROMPTING:
            self.buttons.add_item(self.prompt_button)
            self.buttons.add_item(self.cancel_button)
        elif self.phase in (BattlePhase.THINKING, BattlePhase.RESULT):
            self.buttons.add_item(self.cancel_button)
        # No buttons in FINISHED phase

        if self.buttons.children:
            self.add_item(self.buttons)

        # Footer
        if self.active:
            timeout = datetime.now() + timedelta(seconds=BATTLE_TIMEOUT)
            self.add_item(TextDisplay(f"-# This battle will timeout {format_dt(timeout, style='R')}."))

        if self.cancelled:
            self.add_item(TextDisplay("## The battle has been cancelled."))

    # ==== AI Integration ====

    def _extract_text(self, output: object) -> str:
        """Extract text content from a Mistral conversation output entry."""
        if isinstance(output, MessageOutputEntry):
            if isinstance(output.content, str):
                return output.content
            if isinstance(output.content, list):
                parts: list[str] = []
                for chunk in output.content:
                    if isinstance(chunk, TextChunk):
                        parts.append(chunk.text)
                return "".join(parts)
        return str(output)

    def _describe_team(self, user: BattleUser) -> str:
        """Build a detailed team description with stats and abilities for the AI."""
        lines: list[str] = []
        for b in user.proposal:
            ball = b.countryball
            line = f"- {ball.country}: ATK {b.attack}, HP {b.health}"
            if ball.capacity_name:
                line += f", Ability: {ball.capacity_name} ({ball.capacity_description})"
            lines.append(line)
        return "\n".join(lines)

    async def call_ai(self):
        """Send battle context to the Mistral agent and handle the response."""
        current_prompts = (
            f"Turn {self.turn}:\n"
            f"Player 1 ({self.user1.user.display_name}): {self.user1.current_prompt}\n"
            f"Player 2 ({self.user2.user.display_name}): {self.user2.current_prompt}"
        )

        try:
            loop = asyncio.get_running_loop()
            if self.conversation_id is None:
                # First turn: include team compositions so the agent knows the decks
                team_info = (
                    f"Player 1: {self.user1.user.display_name}\n"
                    f"Team:\n{self._describe_team(self.user1)}\n\n"
                    f"Player 2: {self.user2.user.display_name}\n"
                    f"Team:\n{self._describe_team(self.user2)}\n\n"
                    f"{current_prompts}"
                )
                response = await loop.run_in_executor(
                    _mistral_executor,
                    lambda: self.client.beta.conversations.start(
                        agent_id=MISTRAL_AGENT_ID,
                        inputs=team_info,
                    ),
                )
                self.conversation_id = response.conversation_id
            else:
                response = await loop.run_in_executor(
                    _mistral_executor,
                    lambda: self.client.beta.conversations.append(
                        conversation_id=self.conversation_id,
                        inputs=current_prompts,
                    ),
                )

            ai_text = self._extract_text(response.outputs[-1])
            self.ai_response = ai_text
            self.battle_log.append(
                {
                    "turn": str(self.turn),
                    "player1_prompt": self.user1.current_prompt or "",
                    "player2_prompt": self.user2.current_prompt or "",
                    "ai_response": ai_text,
                }
            )
            self._parse_result(ai_text)
        except Exception:
            log.exception("AI API error during battle")
            self.ai_response = "An error occurred while contacting the AI. You may try again this turn."
            self.phase = BattlePhase.PROMPTING
            self.user1.reset_prompt()
            self.user2.reset_prompt()
            self._start_prompt_timeout()
            await self.edit_message(None)
            return

        if self.result in (BattleResult.PLAYER1_WON, BattleResult.PLAYER2_WON, BattleResult.ENDED):
            self.phase = BattlePhase.FINISHED
            self.stop()
            await self.cleanup()
            await self.edit_message(None)
            await self._upload_battle_log()
            return
        elif self.result == BattleResult.NEXT_TURN:
            self.phase = BattlePhase.RESULT
            await self.edit_message(None)
            # Brief pause to let players read the result before next turn
            await asyncio.sleep(3)
            self.turn += 1
            self.user1.reset_prompt()
            self.user2.reset_prompt()
            self.phase = BattlePhase.PROMPTING
            self._start_prompt_timeout()
        else:
            # No recognized ending, treat as next turn
            self.turn += 1
            self.user1.reset_prompt()
            self.user2.reset_prompt()
            self.phase = BattlePhase.PROMPTING
            self._start_prompt_timeout()

        await self.edit_message(None)

    def _parse_result(self, response: str):
        """Parse the AI response for ending keywords (check last line first, then full text)."""
        lines = response.strip().split("\n")
        last_line = lines[-1].upper() if lines else ""

        if "PLAYER 1 WON" in last_line:
            self.result = BattleResult.PLAYER1_WON
        elif "PLAYER 2 WON" in last_line:
            self.result = BattleResult.PLAYER2_WON
        elif "ENDED" in last_line:
            self.result = BattleResult.ENDED
        elif "NEXT TURN" in last_line:
            self.result = BattleResult.NEXT_TURN
        # Fallback: check full response
        elif "PLAYER 1 WON" in response.upper():
            self.result = BattleResult.PLAYER1_WON
        elif "PLAYER 2 WON" in response.upper():
            self.result = BattleResult.PLAYER2_WON
        elif "ENDED" in response.upper():
            self.result = BattleResult.ENDED
        else:
            self.result = BattleResult.NEXT_TURN

    # ==== Battle log ====

    def _build_log_text(self) -> str:
        """Build a full text log of the battle."""
        lines: list[str] = []
        lines.append(
            f"Battle: {self.user1.user.display_name} vs {self.user2.user.display_name}"
        )
        lines.append(f"Result: {self.result.value if self.result else 'Unknown'}")
        lines.append("")

        lines.append(f"== {self.user1.user.display_name}'s team ==")
        lines.append(self._describe_team(self.user1))
        lines.append("")

        lines.append(f"== {self.user2.user.display_name}'s team ==")
        lines.append(self._describe_team(self.user2))
        lines.append("")

        for entry in self.battle_log:
            lines.append(f"--- Turn {entry['turn']} ---")
            lines.append(f"{self.user1.user.display_name}: {entry['player1_prompt']}")
            lines.append(f"{self.user2.user.display_name}: {entry['player2_prompt']}")
            lines.append("")
            lines.append(f"AI: {entry['ai_response']}")
            lines.append("")

        return "\n".join(lines)

    async def _upload_battle_log(self):
        """Upload the full battle log as a text file."""
        if not self.battle_log:
            return
        log_text = self._build_log_text()
        file = discord.File(
            io.BytesIO(log_text.encode("utf-8")),
            filename=f"battle_log_{self.user1.user.id}_vs_{self.user2.user.id}.txt",
        )
        await self.message.reply(file=file)

    # ==== Timeout handling ====

    async def _timeout(self):
        await asyncio.sleep(BATTLE_TIMEOUT)
        if self.active:
            self.phase = BattlePhase.FINISHED
            await self.cleanup()
            self.add_item(TextDisplay("## The battle has timed out."))
            await self.message.edit(view=self)

    def _start_prompt_timeout(self):
        """Start a timeout for the prompt phase."""
        if self.prompt_timeout_task and not self.prompt_timeout_task.done():
            self.prompt_timeout_task.cancel()
        self.prompt_timeout_task = asyncio.create_task(self._prompt_timeout(), name=f"battle-prompt-timeout-{id(self)}")

    async def _prompt_timeout(self):
        await asyncio.sleep(PROMPT_TIMEOUT)
        if self.phase == BattlePhase.PROMPTING and self.active:
            self.phase = BattlePhase.FINISHED
            await self.cleanup()
            timed_out_users = []
            if not self.user1.prompt_submitted:
                timed_out_users.append(self.user1.user.display_name)
            if not self.user2.prompt_submitted:
                timed_out_users.append(self.user2.user.display_name)
            names = " and ".join(timed_out_users)
            self.ai_response = f"{names} did not submit a prompt in time."
            await self._rebuild_and_edit(None)

    # ==== Cleanup ====

    async def cleanup(self):
        """Cancel all tasks, close the Mistral client, remove from cog tracking, and stop the view."""
        self.timeout_task.cancel()
        if self.prompt_timeout_task and not self.prompt_timeout_task.done():
            self.prompt_timeout_task.cancel()
        try:
            self.client.__exit__(None, None, None)
        except Exception:
            log.warning("Failed to close Mistral client", exc_info=True)
        self.stop()
        # Remove from the cog's battle tracking so players aren't stuck
        channel_id = self.original_interaction.channel_id
        guild_id = self.original_interaction.guild_id
        battles = self.cog.battles
        if guild_id in battles and channel_id in battles[guild_id]:
            try:
                battles[guild_id][channel_id].remove(self)
            except ValueError:
                pass
