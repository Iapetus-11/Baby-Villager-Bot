from concurrent.futures import ThreadPoolExecutor
from statcord import StatcordClusterClient
from collections import defaultdict
from classyjson import ClassyDict
from discord.ext import commands
import pyximport
import aiohttp
import asyncio
import discord
import psutil
import random
import arrow
import numpy

from trusted_contributors import discord_ids

from util.setup import villager_bot_intents, setup_logging, setup_database_pool
from util.cooldowns import CommandOnKarenCooldown, MaxKarenConcurrencyReached
from util.ipc import Client, PacketType, PacketHandlerRegistry, handle_packet
from util.misc import TTLPreventDuplicate, update_support_member_role
from util.setup import load_text, load_secrets, load_data
from util.code import execute_code, format_exception
from util.ctx import BetterContext


def run_cluster(shard_count: int, shard_ids: list, max_db_pool_size: int) -> None:
    # add cython support, with numpy header files
    pyximport.install(language_level=3, setup_args={"include_dirs": numpy.get_include()})

    # for some reason, asyncio tries to use the event loop from the main process
    asyncio.set_event_loop(asyncio.new_event_loop())

    cluster = VillagerBotCluster(shard_count, shard_ids, max_db_pool_size)

    try:
        cluster.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        cluster.logger.error(format_exception(e))


class VillagerBotCluster(commands.AutoShardedBot, PacketHandlerRegistry):
    def __init__(self, shard_count: int, shard_ids: list, max_db_pool_size: int):
        commands.AutoShardedBot.__init__(
            self,
            # status=discord.Status.invisible,
            command_prefix=self.get_prefix,
            case_insensitive=True,
            intents=villager_bot_intents(),
            shard_count=shard_count,
            shard_ids=shard_ids,
            help_command=None,
            owner_ids=discord_ids,
        )

        self.max_db_pool_size = max_db_pool_size

        self.start_time = arrow.utcnow()

        self.k = load_secrets()
        self.d = load_data()
        self.l = load_text()

        self.cog_list = [
            "cogs.core.database",
            "cogs.core.events",
            "cogs.core.loops",
            "cogs.core.badges",
            "cogs.core.mobs",
            "cogs.commands.owner",
            "cogs.commands.useful",
            "cogs.commands.config",
            "cogs.commands.econ",
            "cogs.commands.minecraft",
            "cogs.commands.mod",
            "cogs.commands.fun",
        ]

        # check if this is the first cluster loaded
        # if 0 in shard_ids:
        #     self.cog_list.append("cogs.core.topgg")

        self.logger = setup_logging(self.shard_ids)
        self.ipc = Client(self.k.manager.host, self.k.manager.port, self.get_packet_handlers())  # ipc client
        self.aiohttp = aiohttp.ClientSession()
        self.statcord = None  # StatcordClusterClient instance
        self.db = None  # asyncpg database connection pool
        self.tp = None  # ThreadPoolExecutor instance
        self.prevent_spawn_duplicates = TTLPreventDuplicate(25, 10)

        # caches
        self.ban_cache = set()  # {user_id, user_id,..}
        self.language_cache = {}  # {guild_id: "lang"}
        self.prefix_cache = {}  # {guild_id: "prefix"}
        self.disabled_commands = defaultdict(set)  # {guild_id: {command, command,..}}
        self.replies_cache = set()  # {guild_id, guild_id,..}
        self.rcon_cache = {}  # {(user_id, mc_server): rcon_client}
        self.filter_words_cache = {}  # {guild_id: ["keyword", "keyword"]}

        # support server channels
        self.error_channel = None
        self.vote_channel = None

        # counters and other things
        self.command_count = 0
        self.message_count = 0
        self.error_count = 0
        self.session_votes = 0
        self.after_ready_ready = asyncio.Event()

        self.add_check(self.check_global)  # register global check
        self.before_invoke(self.before_command_invoked)  # register self.before_command_invoked as a before_invoked event
        self.after_invoke(self.after_command_invoked)  # register self.after_command_invoked as a after_invoked event

    @property
    def eval_env(self):  # used in the eval and exec packets and the eval commands
        return {
            **globals(),
            "bot": self,
            "self": self,
            "k": self.k,
            "d": self.d,
            "l": self.l,
            "aiohttp": self.aiohttp,
            "db": self.db,
        }

    async def start(self, *args, **kwargs):
        await self.ipc.connect(self.k.manager.auth)
        self.db = await setup_database_pool(self.k, self.max_db_pool_size)
        asyncio.create_task(self.prevent_spawn_duplicates.run())

        # self.statcord = StatcordClusterClient(self, self.k.statcord, ".".join(map(str, self.shard_ids)))

        for cog in self.cog_list:
            self.load_extension(cog)

        await super().start(*args, **kwargs)

    async def close(self, *args, **kwargs):
        await self.ipc.close()
        await self.db.close()
        await self.aiohttp.close()

        self.statcord.close()

        await super().close(*args, **kwargs)

    def run(self, *args, **kwargs):
        with ThreadPoolExecutor() as self.tp:
            super().run(self.k.discord_token, *args, **kwargs)

    async def get_prefix(self, ctx: commands.Context) -> str:
        # for some reason discord.py wants this function to be async *sigh*

        if ctx.guild:
            return self.prefix_cache.get(ctx.guild.id, self.d.default_prefix)

        return self.d.default_prefix

    def get_language(self, ctx: commands.Context) -> ClassyDict:
        if ctx.guild:
            return self.l[self.language_cache.get(ctx.guild.id, "en")]

        return self.l["en"]

    async def get_context(self, *args, **kwargs) -> BetterContext:
        ctx = await super().get_context(*args, **kwargs, cls=BetterContext)

        ctx.embed_color = self.d.cc
        ctx.l = self.get_language(ctx)

        return ctx

    async def send_embed(self, location, message: str, *, ignore_exceptions: bool = False) -> None:
        try:
            await location.send(embed=discord.Embed(color=self.d.cc, description=message))
        except (discord.errors.Forbidden, discord.errors.HTTPException):
            if not ignore_exceptions:
                raise

    async def reply_embed(self, location, message: str, ping: bool = False, *, ignore_exceptions: bool = False) -> None:
        try:
            await location.reply(embed=discord.Embed(color=self.d.cc, description=message), mention_author=ping)
        except discord.errors.HTTPException as e:
            if e.code == 50035:  # invalid form body, happens sometimes when the message to reply to can't be found?
                await self.send_embed(location, message, ignore_exceptions=ignore_exceptions)
            elif not ignore_exceptions:
                raise
        except discord.errors.Forbidden:
            if not ignore_exceptions:
                raise

    async def send_tip(self, ctx) -> None:
        await asyncio.sleep(random.randint(100, 200) / 100)
        await self.send_embed(ctx, f"{random.choice(ctx.l.misc.tip_intros)} {random.choice(ctx.l.misc.tips)}")

    async def check_global(self, ctx) -> bool:  # the global command check
        self.command_count += 1

        command = str(ctx.command)

        if ctx.author.id in self.ban_cache:
            ctx.failure_reason = "bot_banned"
            return False

        if not self.is_ready():
            ctx.failure_reason = "not_ready"
            return False

        if ctx.guild is not None and command in self.disabled_commands.get(ctx.guild.id, ()):
            ctx.failure_reason = "disabled"
            return False

        # handle cooldowns that need to be synced between shard groups / processes (aka karen cooldowns)
        if command in self.d.cooldown_rates:
            cooldown_info = await self.ipc.request({"type": PacketType.COOLDOWN, "command": command, "user_id": ctx.author.id})

            if not cooldown_info.can_run:
                ctx.custom_error = CommandOnKarenCooldown(cooldown_info.remaining)
                return False

        if command in self.d.concurrency_limited:
            res = await self.ipc.request({"type": PacketType.CONCURRENCY_CHECK, "command": command, "user_id": ctx.author.id})

            if not res.can_run:
                ctx.custom_error = MaxKarenConcurrencyReached()
                return False

        # handle paused econ users
        if ctx.command.cog_name == "Econ":
            # check if user has paused econ
            res = await self.ipc.eval(f"econ_paused_users.get({ctx.author.id})")

            if res.result is not None:
                ctx.failure_reason = "econ_paused"
                return False

            if random.randint(0, self.d.mob_chance) == 0:  # spawn mob?
                if self.d.cooldown_rates.get(command, 0) >= 2:
                    if not self.prevent_spawn_duplicates.check(ctx.channel.id):
                        self.prevent_spawn_duplicates.put(ctx.channel.id)
                        asyncio.create_task(self.get_cog("MobSpawner").spawn_event(ctx))
            elif random.randint(0, self.d.tip_chance) == 0:  # send tip?
                asyncio.create_task(self.send_tip(ctx))

        asyncio.create_task(self.ipc.send({"type": PacketType.COMMAND_RAN, "user_id": ctx.author.id}))

        return True

    async def before_command_invoked(self, ctx):
        try:
            if str(ctx.command) in self.d.concurrency_limited:
                await self.ipc.send(
                    {"type": PacketType.CONCURRENCY_ACQUIRE, "command": str(ctx.command), "user_id": ctx.author.id}
                )
        except Exception as e:
            self.logger.error(format_exception(e))
            raise

    async def after_command_invoked(self, ctx):
        try:
            if str(ctx.command) in self.d.concurrency_limited:
                await self.ipc.send(
                    {"type": PacketType.CONCURRENCY_RELEASE, "command": str(ctx.command), "user_id": ctx.author.id}
                )
        except Exception as e:
            self.logger.error(format_exception(e))
            raise

    @handle_packet(PacketType.MISSING_PACKET)
    async def handle_missing_packet(self, packet: ClassyDict):
        try:
            packet_type = PacketType(packet.get("type"))
        except ValueError:
            packet_type = packet.get("type")

        self.logger.error(f"Missing packet handler for packet type {packet_type}")

    @handle_packet(PacketType.EVAL)
    async def handle_eval_packet(self, packet: ClassyDict):
        try:
            result = eval(packet.code, self.eval_env)
            success = True
        except Exception as e:
            result = format_exception(e)
            success = False

            self.logger.error(result)

        return {"result": result, "success": success}

    @handle_packet(PacketType.EXEC)
    async def handle_exec_packet(self, packet: ClassyDict):
        try:
            result = await execute_code(packet.code, self.eval_env)
            success = True
        except Exception as e:
            result = format_exception(e)
            success = False

            self.logger.error(result)

        return {"result": result, "success": success}

    @handle_packet(PacketType.REMINDER)
    async def handle_reminder_packet(self, packet: ClassyDict):
        success = False
        channel = self.get_channel(packet.channel_id)

        if channel is not None:
            user = self.get_user(packet.user_id)

            if user is not None:
                lang = self.get_language(channel)

                try:
                    message = await channel.fetch_message(packet.message_id)
                    await message.reply(lang.useful.remind.reminder.format(user.mention, packet.reminder), mention_author=True)
                    success = True
                except Exception:
                    try:
                        await channel.send(lang.useful.remind.reminder.format(user.mention, packet.reminder))
                        success = True
                    except Exception as e:
                        self.logger.error(format_exception(e))

        return {"success": success}

    @handle_packet(PacketType.FETCH_STATS)
    async def handle_fetch_stats_packet(self, packet: ClassyDict):
        proc = psutil.Process()
        with proc.oneshot():
            mem_usage = proc.memory_full_info().uss
            threads = proc.num_threads()

        return {
            "stats": [
                mem_usage,
                threads,
                len(asyncio.all_tasks()),
                len(self.guilds),
                len(self.users),
                self.message_count,
                self.command_count,
                self.latency,
                len(self.private_channels),
                self.session_votes,
            ],
        }

    @handle_packet(PacketType.UPDATE_SUPPORT_SERVER_ROLES)
    async def handle_update_support_server_roles_packet(self, packet: ClassyDict):
        success = False

        try:
            support_guild = self.get_guild(self.d.support_server_id)

            if support_guild is not None:
                member = support_guild.get_member(packet.user)

                if member is not None:
                    await update_support_member_role(self, member)
                    success = True
        finally:
            return {"success": success}
