import os
import random
import discord
from aleph_alpha_client import AlephAlphaClient
from aleph_alpha_client import AlephAlphaModel
from aleph_alpha_client import CompletionRequest
from aleph_alpha_client import ImagePrompt
from aleph_alpha_client import Prompt
from discord.ext import commands
from marsbots.discord_utils import is_mentioned
from marsbots.discord_utils import replace_bot_mention
from marsbots.discord_utils import replace_mentions_with_usernames
from marsbots.language_models import OpenAIGPT3LanguageModel
from marsbots_eden.eden import generation_loop
from marsbots_eden.models import SourceSettings
from marsbots_eden.models import StableDiffusionConfig

from . import config
from . import settings


MINIO_URL = "http://{}/{}".format(os.getenv("MINIO_URL"), os.getenv("BUCKET_NAME"))
GATEWAY_URL = os.getenv("GATEWAY_URL")
MAGMA_TOKEN = os.getenv("MAGMA_API_KEY")

CONFIG = config.config_dict[config.stage]
ALLOWED_GUILDS = CONFIG["guilds"]
ALLOWED_CHANNELS = CONFIG["allowed_channels"]


class EdenCog(commands.Cog):
    def __init__(self, bot: commands.bot) -> None:
        self.bot = bot
        self.language_model = OpenAIGPT3LanguageModel(
            engine=settings.GPT3_ENGINE,
            temperature=settings.GPT3_TEMPERATURE,
            frequency_penalty=settings.GPT3_FREQUENCY_PENALTY,
            presence_penalty=settings.GPT3_PRESENCE_PENALTY,
        )
        self.magma_model = AlephAlphaModel(
            AlephAlphaClient(host="https://api.aleph-alpha.com", token=MAGMA_TOKEN),
            model_name="luminous-extended",
        )

    @commands.slash_command(guild_ids=ALLOWED_GUILDS)
    async def dream(
        self,
        ctx,
        text_input: discord.Option(str, description="Prompt", required=True),
        aspect_ratio: discord.Option(
            str,
            choices=[
                discord.OptionChoice(name="square", value="square"),
                discord.OptionChoice(name="landscape", value="landscape"),
                discord.OptionChoice(name="portrait", value="portrait")
            ],
            required=False,
            default="square"
        ),
        large: discord.Option(bool, description="Larger resolution, ~2.25x more pixels", required=False, default=False),
        fast: discord.Option(bool, description="Fast generation, possibly some loss of quality", required=False, default=False)
    ):
        
        if not self.perm_check(ctx):
            await ctx.respond("This command is not available in this channel.")
            return
        
        if settings.CONTENT_FILTER_ON:
            if not OpenAIGPT3LanguageModel.content_safe(text_input):
                await ctx.respond(
                    f"Content filter triggered, <@!{ctx.author.id}>. Please don't make me draw that. If you think it was a mistake, modify your prompt slightly and try again.",
                )
                return
        
        source = SourceSettings(
            origin="discord",
            author=int(ctx.author.id),
            author_name=str(ctx.author),
            guild=int(ctx.guild.id),
            guild_name=str(ctx.guild),
            channel=int(ctx.channel.id),
            channel_name=str(ctx.channel),
        )
        
        width, height = self.get_dimensions(aspect_ratio, large)
        ddim_steps = 15 if fast else 50
        
        config = StableDiffusionConfig(
            mode='generate',
            text_input=text_input,
            width=width,
            height=height,
            ddim_steps=ddim_steps,
            seed=random.randint(1,1e8)
        )
        
        start_bot_message = f"**{text_input}** - <@!{ctx.author.id}>\n"
        await ctx.respond(start_bot_message)
        
        async def self_run_again():
            await self.dream(ctx, text_input, aspect_ratio, large, fast)

        await generation_loop(
            GATEWAY_URL,
            MINIO_URL,
            ctx,
            start_bot_message,
            source,
            config,
            refresh_action=self_run_again,
            refresh_interval=2
        )

    @commands.slash_command(guild_ids=ALLOWED_GUILDS)
    async def lerp(
        self,
        ctx,
        text_input1: discord.Option(str, description="First prompt", required=True),
        text_input2: discord.Option(str, description="Second prompt", required=True),
        aspect_ratio: discord.Option(
            str,
            choices=[
                discord.OptionChoice(name="square", value="square"),
                discord.OptionChoice(name="landscape", value="landscape"),
                discord.OptionChoice(name="portrait", value="portrait")
            ],
            required=False,
            default="square"
        )
    ):

        if not self.perm_check(ctx):
            await ctx.respond("This command is not available in this channel.")
            return

        if settings.CONTENT_FILTER_ON:
            if not OpenAIGPT3LanguageModel.content_safe(text_input1) or \
               not OpenAIGPT3LanguageModel.content_safe(text_input2):
                await ctx.respond(
                    f"Content filter triggered, <@!{ctx.author.id}>. Please don't make me draw that. If you think it was a mistake, modify your prompt slightly and try again.",
                )
                return

        source = SourceSettings(
            origin="discord",
            author=int(ctx.author.id),
            author_name=str(ctx.author),
            guild=int(ctx.guild.id),
            guild_name=str(ctx.guild),
            channel=int(ctx.channel.id),
            channel_name=str(ctx.channel),
        )

        interpolation_texts = [text_input1, text_input2]
        n_interpolate = 12
        ddim_steps = 25
        width, height = self.get_dimensions(aspect_ratio, False)
        
        config = StableDiffusionConfig(
            mode='interpolate',
            text_input=text_input1,
            interpolation_texts=interpolation_texts,
            n_interpolate=n_interpolate,
            width=width,
            height=height,
            ddim_steps=ddim_steps,
            seed=random.randint(1,1e8),
            fixed_code=True
        )

        start_bot_message = f"**{text_input1}** to **{text_input2}** - <@!{ctx.author.id}>\n"
        await ctx.respond(start_bot_message)

        await generation_loop(
            GATEWAY_URL,
            MINIO_URL,
            ctx,
            start_bot_message,
            source,
            config,
            refresh_interval=2
        )

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message) -> None:
        try:
            if (
                message.channel.id not in ALLOWED_CHANNELS
                or message.author.id == self.bot.user.id
                or message.author.bot
            ):
                return

            trigger_reply = (
                is_mentioned(message, self.bot.user) 
                and message.attachments
            )

            if trigger_reply:
                ctx = await self.bot.get_context(message)
                async with ctx.channel.typing():
                    prompt = self.message_preprocessor(message)
                    stop_sequences = []
                    if prompt:
                        text_input = 'Question: "{}"\nAnswer:'.format(prompt)
                        stop_sequences = ['Question:']
                        prefix = ""
                    else:
                        text_input = "This is a picture of "
                        prefix = text_input
                    url = message.attachments[0].url
                    image = ImagePrompt.from_url(url)
                    magma_prompt = Prompt([image, text_input])
                    request = CompletionRequest(
                        prompt=magma_prompt, 
                        maximum_tokens=100,
                        temperature=0.5,
                        stop_sequences=stop_sequences
                    )
                    result = self.magma_model.complete(request)
                    response = prefix + result.completions[0].completion.strip(' "')
                    await message.reply(response)

        except Exception as e:
            print(f"Error: {e}")
            await message.reply(":)")

    def message_preprocessor(self, message: discord.Message) -> str:
        message_content = replace_bot_mention(message.content, only_first=True)
        message_content = replace_mentions_with_usernames(message_content, message.mentions)
        message_content = message_content.strip()
        return message_content

    def get_dimensions(self, aspect_ratio, large):
        if aspect_ratio == 'square' and large:
            width, height = 768, 768
        elif aspect_ratio == 'square' and not large:
            width, height = 512, 512
        elif aspect_ratio == 'landscape' and large:
            width, height = 896, 640
        elif aspect_ratio == 'landscape' and not large:
            width, height = 640, 384
        elif aspect_ratio == 'portrait' and large:
            width, height = 640, 896
        elif aspect_ratio == 'portrait' and not large:
            width, height = 384, 640
        return width, height

    def perm_check(self, ctx):
        if ctx.channel.id not in ALLOWED_CHANNELS:
            return False
        return True


def setup(bot: commands.Bot) -> None:
    bot.add_cog(EdenCog(bot))
