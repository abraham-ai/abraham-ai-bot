import asyncio
import os
import random
from dataclasses import dataclass
from typing import Optional

import discord
import requests
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
from marsbots_eden.eden import get_file_update
from marsbots_eden.eden import poll_creation_queue
from marsbots_eden.eden import request_creation
from marsbots_eden.models import SignInCredentials
from marsbots_eden.models import SourceSettings
from marsbots_eden.models import StableDiffusionConfig

from . import config
from . import settings


# MINIO_URL = "https://{}/{}".format(os.getenv("MINIO_URL"), os.getenv("BUCKET_NAME"))
MINIO_URL = "https://{}/{}".format(os.getenv("MINIO_URL"), "creations-stg")
GATEWAY_URL = "https://gateway-test.abraham.ai"  # os.getenv("GATEWAY_URL")
MAGMA_TOKEN = os.getenv("MAGMA_API_KEY")
EDEN_API_KEY = os.getenv("EDEN_API_KEY")
EDEN_API_SECRET = os.getenv("EDEN_API_SECRET")

CONFIG = config.config_dict[config.stage]
ALLOWED_GUILDS = CONFIG["guilds"]
ALLOWED_CHANNELS = CONFIG["allowed_channels"]
ALLOWED_LERP_BACKDOOR_USERS = CONFIG["allowed_channels"]


@dataclass
class GenerationLoopInput:
    gateway_url: str
    minio_url: str
    start_bot_message: str
    source: SourceSettings
    config: any
    message: discord.Message
    is_video_request: bool = False
    prefer_gif: bool = True
    refresh_interval: int = 2
    parent_message: discord.Message = None


class LerpModal(discord.ui.Modal):
    def __init__(self, bot, refresh_callback, loop_input, **kwargs) -> None:
        super().__init__(**kwargs)
        self.bot = bot
        self.refresh_callback = refresh_callback
        self.loop_input = loop_input
        self.add_item(discord.ui.InputText(label="Short Input"))

    async def callback(self, interaction: discord.Interaction):
        ctx = await self.bot.get_application_context(interaction)
        await ctx.defer()
        text_input1 = self.loop_input.config.text_input
        text_input2 = self.children[0].value
        interpolation_texts = [text_input1, text_input2]
        seed1 = self.loop_input.config.seed
        seed2 = random.randint(1, 1e8)
        interpolation_seeds = [seed1, seed2]
        width = self.loop_input.config.width
        height = self.loop_input.config.height
        n_frames = 32
        steps = self.loop_input.config.steps
        self.loop_input.config = StableDiffusionConfig(
            mode="interpolate",
            stream=True,
            stream_every=1,
            text_input=text_input1,
            interpolation_texts=interpolation_texts,
            interpolation_seeds=interpolation_seeds,
            n_frames=n_frames,
            width=width,
            height=height,
            steps=steps,
        )
        self.loop_input.is_video_request = True
        await self.refresh_callback(loop_input=self.loop_input, reroll_seed=False)


class CreationActionButtons(discord.ui.View):
    def __init__(
        self,
        *items,
        bot,
        creation_sha,
        refresh_callback,
        loop_input: GenerationLoopInput,
        timeout=180,
    ):
        super().__init__(*items, timeout=timeout)
        self.bot = bot
        self.creation_sha = creation_sha
        self.refresh_callback = refresh_callback
        self.loop_input = loop_input

    async def feedback(self, stat, interaction):
        ctx = await self.bot.get_application_context(interaction)
        await ctx.defer()
        requests.post(
            self.loop_input.gateway_url + "/update_stats",
            json={
                "creation": self.creation_sha,
                "stat": stat,
                "opperation": "increase",
                "address": interaction.user.id,
            },
        )

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.blurple)
    async def refresh(self, button, interaction):
        ctx = await self.bot.get_application_context(interaction)
        await ctx.defer()
        await self.refresh_callback(
            loop_input=self.loop_input,
        )

    @discord.ui.button(label="Lerp It")
    async def lerp(self, button, interaction):
        await interaction.response.send_modal(
            LerpModal(
                title="Lerp It",
                bot=self.bot,
                refresh_callback=self.refresh_callback,
                loop_input=self.loop_input,
            )
        )

    @discord.ui.button(emoji="🔥", style=discord.ButtonStyle.red)
    async def burn(self, button, interaction):
        await self.feedback("burn", interaction)

    @discord.ui.button(label="🙌", style=discord.ButtonStyle.green)
    async def praise(self, button, interaction):
        await self.feedback("praise", interaction)
        self.stop()


class EdenCog(commands.Cog):
    def __init__(self, bot: commands.bot) -> None:
        self.bot = bot
        self.eden_credentials = SignInCredentials(
            apiKey=EDEN_API_KEY, apiSecret=EDEN_API_SECRET
        )
        self.language_model = OpenAIGPT3LanguageModel(
            engine=settings.GPT3_ENGINE,
            temperature=settings.GPT3_TEMPERATURE,
            frequency_penalty=settings.GPT3_FREQUENCY_PENALTY,
            presence_penalty=settings.GPT3_PRESENCE_PENALTY,
        )
        # self.magma_model = AlephAlphaModel(
        #     AlephAlphaClient(host="https://api.aleph-alpha.com", token=MAGMA_TOKEN),
        #     model_name="luminous-extended",
        # )

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
                discord.OptionChoice(name="portrait", value="portrait"),
            ],
            required=False,
            default="square",
        ),
        large: discord.Option(
            bool,
            description="Larger resolution, ~2.25x more pixels",
            required=False,
            default=False,
        ),
        fast: discord.Option(
            bool,
            description="Fast generation, possibly some loss of quality",
            required=False,
            default=False,
        ),
    ):

        print("Received dream:", text_input)

        if not self.perm_check(ctx):
            await ctx.respond("This command is not available in this channel.")
            return

        if settings.CONTENT_FILTER_ON:
            if not OpenAIGPT3LanguageModel.content_safe(text_input):
                await ctx.respond(
                    f"Content filter triggered, <@!{ctx.author.id}>. Please don't make me draw that. If you think it was a mistake, modify your prompt slightly and try again.",
                )
                return

        source = self.get_source(ctx)
        width, height, upscale_f = self.get_dimensions(aspect_ratio, large, img_mode = True)
        steps = 30 if fast else 60

        config = StableDiffusionConfig(
            mode="generate",
            stream=True,
            stream_every=4,
            text_input=text_input,
            sampler="euler_ancestral",
            uc_text="poorly drawn face, ugly, tiling, out of frame, extra limbs, disfigured, deformed body, blurry, blurred, watermark, text, grainy, signature, cut off, draft",
            width=width,
            height=height,
            steps=steps,
            upscale_f=upscale_f,
            seed=random.randint(1, 1e8),
        )

        start_bot_message = f"**{text_input}** - <@!{ctx.author.id}>\n"
        await ctx.respond("Starting to dream...")
        message = await ctx.channel.send(start_bot_message)

        generation_loop_input = GenerationLoopInput(
            gateway_url=GATEWAY_URL,
            minio_url=MINIO_URL,
            message=message,
            start_bot_message=start_bot_message,
            source=source,
            config=config,
        )
        await self.generation_loop(generation_loop_input)

    @commands.slash_command(guild_ids=ALLOWED_GUILDS)
    async def remix(
        self,
        ctx,
        image1: discord.Option(
            discord.Attachment, description="Image to remix", required=True
        ),
    ):

        print("Received remix:", image1)

        if not self.perm_check(ctx):
            await ctx.respond("This command is not available in this channel.")
            return

        if not image1:
            await ctx.respond("Please provide an image to remix.")
            return

        source = self.get_source(ctx)

        steps = 100
        width, height = 1280, 720

        config = StableDiffusionConfig(
            mode="remix",
            stream=True,
            stream_every=1,
            text_input="remix",
            uc_text="poorly drawn face, ugly, tiling, out of frame, extra limbs, disfigured, deformed body, blurry, blurred, watermark, text, grainy, signature, cut off, draft",
            init_image_data=image1.url,
            width=width,
            height=height,
            sampler="euler_ancestral", 
            steps=steps,
            seed=random.randint(1, 1e8)
        )

        start_bot_message = f"**Remix** by <@!{ctx.author.id}>\n"
        await ctx.respond("Remixing...")
        message = await ctx.channel.send(start_bot_message)

        generation_loop_input = GenerationLoopInput(
            gateway_url=GATEWAY_URL,
            minio_url=MINIO_URL,
            message=message,
            start_bot_message=start_bot_message,
            source=source,
            config=config,
            is_video_request=False,
            prefer_gif=False
        )
        await self.generation_loop(generation_loop_input)

    @commands.slash_command(guild_ids=ALLOWED_GUILDS)
    async def real2real(
        self,
        ctx,
        image1: discord.Option(
            discord.Attachment, description="First image", required=True
        ),
        image2: discord.Option(
            discord.Attachment, description="Second image", required=True
        ),
    ):

        if not self.perm_check(ctx):
            await ctx.respond("This command is not available in this channel.")
            return

        source = self.get_source(ctx)

        if not (image1 and image2):
            await ctx.respond("Please provide two images to interpolate between.")
            return

        interpolation_init_images = [image1.url, image2.url]

        interpolation_seeds = [
            random.randint(1, 1e8) for _ in interpolation_init_images
        ]
        n_frames = 32
        steps = 50
        width, height = 578, 578

        config = StableDiffusionConfig(
            mode="interpolate",
            stream=True,
            stream_every=1,
            text_input="real2real",
            uc_text="poorly drawn face, ugly, tiling, out of frame, extra limbs, disfigured, deformed body, blurry, blurred, watermark, text, grainy, signature, cut off, draft",
            interpolation_seeds=interpolation_seeds,
            interpolation_init_images=interpolation_init_images,
            interpolation_init_images_use_img2txt=True,
            n_frames=n_frames,
            loop=False,
            smooth=True,
            n_film=1,
            width=width,
            height=height,
            sampler="klms",
            steps=steps,
            scale=12,
            scale_modulation=0.1,
            latent_smoothing_std=0.01,
            seed=random.randint(1, 1e8),
            aesthetic_steps              = 10,
            aesthetic_lr                 = 0.0001,
            ag_L2_normalization_constant = 0.1, # for real2real, only normalize the aesthetic gradient a tiny bit
        )

        start_bot_message = f"**Real2Real** by <@!{ctx.author.id}>\n"
        await ctx.respond("Lerping...")
        message = await ctx.channel.send(start_bot_message)

        generation_loop_input = GenerationLoopInput(
            gateway_url=GATEWAY_URL,
            minio_url=MINIO_URL,
            message=message,
            start_bot_message=start_bot_message,
            source=source,
            config=config,
            is_video_request=True,
            prefer_gif=False,
        )
        await self.generation_loop(generation_loop_input)

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
                discord.OptionChoice(name="portrait", value="portrait"),
            ],
            required=False,
            default="square",
        ),
    ):

        print("Received lerp:", text_input1, text_input2)

        if not self.perm_check(ctx):
            await ctx.respond("This command is not available in this channel.")
            return

        if settings.CONTENT_FILTER_ON:
            if not OpenAIGPT3LanguageModel.content_safe(
                text_input1,
            ) or not OpenAIGPT3LanguageModel.content_safe(text_input2):
                await ctx.respond(
                    f"Content filter triggered, <@!{ctx.author.id}>. Please don't make me draw that. If you think it was a mistake, modify your prompt slightly and try again.",
                )
                return

        source = self.get_source(ctx)

        interpolation_texts = [text_input1, text_input2]
        interpolation_seeds = [random.randint(1, 1e8) for _ in interpolation_texts]
        n_frames = 32
        steps = 50
        width, height, upscale_f = self.get_dimensions(aspect_ratio, False, img_mode = False)

        config = StableDiffusionConfig(
            mode="interpolate",
            stream=True,
            stream_every=1,
            text_input=text_input1,
            uc_text="poorly drawn face, ugly, tiling, out of frame, extra limbs, disfigured, deformed body, blurry, blurred, watermark, text, grainy, signature, cut off, draft",
            interpolation_texts=interpolation_texts,
            interpolation_seeds=interpolation_seeds,
            n_frames=n_frames,
            smooth=True,
            loop=False,
            n_film=1,
            width=width,
            height=height,
            sampler="klms",
            steps=steps,
            scale=12,
            scale_modulation=0.1,
            latent_smoothing_std=0.01,
            seed=random.randint(1, 1e8),
        )

        start_bot_message = (
            f"**{text_input1}** to **{text_input2}** - <@!{ctx.author.id}>\n"
        )
        await ctx.respond("Lerping...")
        message = await ctx.channel.send(start_bot_message)

        generation_loop_input = GenerationLoopInput(
            gateway_url=GATEWAY_URL,
            minio_url=MINIO_URL,
            message=message,
            start_bot_message=start_bot_message,
            source=source,
            config=config,
            is_video_request=True,
            prefer_gif=False,
        )
        await self.generation_loop(generation_loop_input)

    async def generation_loop(
        self,
        loop_input: GenerationLoopInput,
    ):
        gateway_url = loop_input.gateway_url
        minio_url = loop_input.minio_url
        start_bot_message = loop_input.start_bot_message
        parent_message = loop_input.parent_message
        message = loop_input.message
        source = loop_input.source
        config = loop_input.config
        refresh_interval = loop_input.refresh_interval
        is_video_request = loop_input.is_video_request
        prefer_gif = loop_input.prefer_gif

        try:
            task_id = await request_creation(
                gateway_url, self.eden_credentials, source, config
            )
            current_sha = None
            while True:
                result, file, sha = await poll_creation_queue(
                    gateway_url, minio_url, task_id, is_video_request, prefer_gif
                )
                if sha != current_sha:
                    current_sha = sha
                    message_update = self.get_message_update(result)
                    await self.edit_message(
                        message,
                        start_bot_message,
                        message_update,
                        file_update=file,
                    )
                if result["status"] == "complete":
                    file, sha = await get_file_update(
                        result, minio_url, is_video_request, prefer_gif
                    )
                    view = CreationActionButtons(
                        bot=self.bot,
                        creation_sha=sha,
                        loop_input=loop_input,
                        refresh_callback=self.refresh_callback,
                    )
                    if parent_message:
                        new_message = await parent_message.reply(
                            start_bot_message,
                            files=[file],
                            view=None,
                        )
                    else:
                        new_message = await message.channel.send(
                            start_bot_message,
                            files=[file],
                            view=None,
                        )
                    view.loop_input.parent_message = new_message
                    await message.delete()
                    return
                await asyncio.sleep(refresh_interval)

        except Exception as e:
            await self.edit_message(message, start_bot_message, f"Error: {e}")

    async def refresh_callback(
        self,
        loop_input: GenerationLoopInput,
        reroll_seed: bool = True,
    ):
        loop_input.message = await loop_input.parent_message.reply(
            loop_input.start_bot_message,
        )
        if reroll_seed:
            loop_input.config.seed = random.randint(1, 1e8)
        await self.generation_loop(loop_input)

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message) -> None:
        try:
            if (
                message.channel.id not in ALLOWED_CHANNELS
                or message.author.id == self.bot.user.id
                or message.author.bot
            ):
                return

            trigger_reply = is_mentioned(message, self.bot.user) and message.attachments

            if trigger_reply:
                ctx = await self.bot.get_context(message)
                async with ctx.channel.typing():
                    prompt = self.message_preprocessor(message)
                    stop_sequences = []
                    if prompt:
                        text_input = 'Question: "{}"\nAnswer:'.format(prompt)
                        stop_sequences = ["Question:"]
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
                        stop_sequences=stop_sequences,
                    )
                    result = self.magma_model.complete(request)
                    response = prefix + result.completions[0].completion.strip(' "')
                    await message.reply(response)

        except Exception as e:
            print(f"Error: {e}")
            await message.reply(":)")

    def message_preprocessor(self, message: discord.Message) -> str:
        message_content = replace_bot_mention(message.content, only_first=True)
        message_content = replace_mentions_with_usernames(
            message_content,
            message.mentions,
        )
        message_content = message_content.strip()
        return message_content

    def get_dimensions(self, aspect_ratio, large, img_mode = True):
        if img_mode:
            if aspect_ratio == "square":
                width, height = 768, 768
            elif aspect_ratio == "landscape":
                width, height = 832, 512
            elif aspect_ratio == "portrait":
                width, height = 512, 832
        else: # video mode:
            if aspect_ratio == "square":
                width, height = 640, 640
            elif aspect_ratio == "landscape":
                width, height = 768, 512
            elif aspect_ratio == "portrait":
                width, height = 512, 768
                
        if large and img_mode:
            upscale_f = 1.4
        else:
            upscale_f = 1.0
            
        return width, height, upscale_f

    def perm_check(self, ctx):
        if ctx.channel.id not in ALLOWED_CHANNELS:
            return False
        return True

    def get_source(self, ctx):
        source = SourceSettings(
            author_id=int(ctx.author.id),
            author_name=str(ctx.author),
            guild_id=int(ctx.guild.id),
            guild_name=str(ctx.guild),
            channel_id=int(ctx.channel.id),
            channel_name=str(ctx.channel),
        )
        return source

    def get_message_update(self, result):
        status = result["status"]
        if status == "failed":
            return "_Server error: Eden task failed_"
        elif status in "pending":
            return "_Warming up, please wait._"
        elif status in "starting":
            return "_Creation is starting_"
        elif status == "running":
            progress = int(100 * result["progress"])
            return f"_Creation is **{progress}%** complete_"
        elif status == "complete":
            return "_Creation is **100%** complete_"

    async def edit_interaction(
        self,
        ctx,
        start_bot_message,
        message_update,
        file_update=None,
    ):
        message_content = f"{start_bot_message}\n{message_update}"
        if file_update:
            await ctx.edit(content=message_content, file=file_update)
        else:
            await ctx.edit(content=message_content)

    async def edit_message(
        self,
        message: discord.Message,
        start_bot_message: str,
        message_update: str,
        file_update: Optional[discord.File] = None,
    ) -> discord.Message:
        message_content = f"{start_bot_message}\n{message_update}"
        await message.edit(content=message_content)
        if file_update:
            await message.edit(files=[file_update], attachments=[])


def setup(bot: commands.Bot) -> None:
    bot.add_cog(EdenCog(bot))
