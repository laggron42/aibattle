from collections import defaultdict
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ballsdex.core.utils.transformers import BallInstanceTransform, SpecialEnabledTransform
from bd_models.models import Player
from settings.models import settings

from .battle import BattleInstance, BattleUser

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot


class AIBattle(commands.GroupCog, group_name="battle"):
    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self.battles: dict[int, dict[int, list[BattleInstance]]] = defaultdict(lambda: defaultdict(list))

    def get_battle(
        self,
        interaction: discord.Interaction | None = None,
        *,
        channel: discord.TextChannel | None = None,
        user: discord.User | discord.Member | None = None,
    ) -> tuple[BattleInstance, BattleUser] | tuple[None, None]:
        """
        Find an ongoing battle for the given interaction.

        Parameters
        ----------
        interaction: discord.Interaction
            The current interaction, used for getting the guild, channel and author.

        Returns
        -------
        tuple[BattleMenu, BattleUser] | tuple[None, None]
            A tuple with the `BattleMenu` and `BattleUser` if found, else `None`.
        """
        guild: discord.Guild
        if interaction:
            guild = interaction.guild
            channel = interaction.channel
            user = interaction.user
        else:
            guild = channel.guild

        if guild.id not in self.battles:
            return (None, None)
        if channel.id not in self.battles[guild.id]:
            return (None, None)
        to_remove: list[BattleInstance] = []
        for battle in self.battles[guild.id][channel.id]:
            if battle.current_view.is_finished() or battle.user1.cancelled or battle.user2.cancelled:
                # remove what was supposed to have been removed
                to_remove.append(battle)
                continue
            try:
                battle_user = battle._get_user(user)
            except RuntimeError:
                continue
            else:
                break
        else:
            for battle in to_remove:
                self.battles[guild.id][channel.id].remove(battle)
            return (None, None)

        for battle in to_remove:
            self.battles[guild.id][channel.id].remove(battle)
        return (battle, battle_user)

    @app_commands.command()
    async def start(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        *,
        duplicates: bool = True,
        amount: int = 3,
    ):
        """
        Begin a battle with the chosen user.

        Parameters
        ----------
        user: discord.User
            The user you want to battle with
        duplicates: bool
            Whether or not you want to allow duplicates in your battle
        amount: int
            The amount of countryballs needed for the battle. Minimum is 3, maximum is 10.
        """
        if user.bot:
            await interaction.response.send_message("You cannot battle with bots.", ephemeral=True)
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message("You cannot battle with yourself.", ephemeral=True)
            return
        if amount < 3 or amount > 10:
            await interaction.response.send_message("You can only battle with 3 to 10 countryballs.", ephemeral=True)
            return

        battle1, user1 = self.get_battle(interaction)
        battle2, user2 = self.get_battle(channel=interaction.channel, user=user)
        if battle1 or user1:
            await interaction.response.send_message("You already have an ongoing battle.", ephemeral=True)
            return
        if battle2 or user2:
            await interaction.response.send_message(
                "The user you are trying to battle with is already in a battle.", ephemeral=True
            )
            return

        player1, _ = await Player.objects.aget_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.objects.aget_or_create(discord_id=user.id)
        battle = BattleInstance(
            self,
            interaction,
            BattleUser(interaction.user, player1),
            BattleUser(user, player2),
            duplicates=duplicates,
            amount=amount,
        )
        self.battles[interaction.guild.id][interaction.channel.id].append(battle)
        await interaction.response.send_message("Battle started!", ephemeral=True)
        await battle.start()

    @app_commands.command()
    async def add(
        self,
        interaction: discord.Interaction,
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Add a countryball to the battle plan.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to add to your proposal
        """
        if not countryball:
            return
        if not countryball.countryball.tradeable:
            await interaction.response.send_message("You cannot battle this countryball.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not countryball.is_tradeable:
            await interaction.followup.send("You cannot use this ball.")
            return

        battle, battle_user = self.get_battle(interaction)
        if not battle or not battle_user:
            await interaction.followup.send("You do not have an ongoing battle.", ephemeral=True)
            return
        if battle_user.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the battle instead.",
                ephemeral=True,
            )
            return
        if countryball in battle_user.proposal:
            await interaction.followup.send(
                f"You already have this {settings.collectible_name} in your proposal.", ephemeral=True
            )
            return
        if len(battle_user.proposal) >= battle.amount:
            await interaction.followup.send(
                f"You cannot have more than {battle.amount} countryballs in your battle plan.", ephemeral=True
            )
            return
        if not battle.duplicates and countryball.countryball in [x.countryball for x in battle_user.proposal]:
            await interaction.followup.send(
                f"You already have a {settings.collectible_name} from this country in your proposal.", ephemeral=True
            )
            return
        battle_user.proposal.append(countryball)
        await interaction.followup.send(f"{countryball.countryball.country} added.", ephemeral=True)
        await battle.edit_message(None)

    @app_commands.command()
    async def remove(
        self,
        interaction: discord.Interaction,
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Remove a countryball from what you proposed in the ongoing battle plan.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to remove from your proposal
        """
        if not countryball:
            return

        battle, battle_user = self.get_battle(interaction)
        if not battle or not battle_user:
            await interaction.response.send_message("You do not have an ongoing battle plan.", ephemeral=True)
            return
        if battle_user.locked:
            await interaction.response.send_message(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the battle instead.",
                ephemeral=True,
            )
            return
        if countryball not in battle_user.proposal:
            await interaction.response.send_message(
                f"That {settings.collectible_name} is not in your proposal.", ephemeral=True
            )
            return
        battle_user.proposal.remove(countryball)
        await interaction.response.send_message(f"{countryball.countryball.country} removed.", ephemeral=True)
        await battle.edit_message(None)
