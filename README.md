# **Baby Villager Bot**

## What is this?
* Baby Villager Bot is a project to simultaneously allow development / brainstorming of new features on [Villager Bot](https://github.com/Iapetus-11/Villager-Bot) and facilitate the teaching and learning of software development skills to those who contribute.
* Users may contribute code that isn't up to the stricter standards of the original repository and while learning can contribute to the future of Villager Bot.
* Trusted users may push changes without submitting pull requests first
* Some features may be taken from this bot and added into the main bot after being polished.

### Development
* If you have any questions or want to become a trusted contributor, please join the [Discord support server](https://discord.gg/39DwwUV) for Villager Bot and use the **#tech-chat** channel.
* Please test before pushing changes, if you break something, you need to fix it.
* You do not need to update the translations, just use a normal string instead of accessing the language dictionary (`ctx.l`).

### Setting up Villager Bot
1. `git clone` Villager Bot, and `cd` into the `Villager-Bot` directory.
2. create a [PostgreSQL](https://www.postgresql.org/) database, and execute the contents of `setup.sql` to create the necessary tables.
3. make a new file called `secrets.json` and fill in the fields based off the `secrets.example.json` file.
4. use [poetry](https://python-poetry.org) to install the required dependencies with `poetry install`.
5. run the bot with either `villager-bot.bat` or `villager-bot.sh`.
