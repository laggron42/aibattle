# Ballsdex AI battle

This is a Ballsdex v3 module made for April fools 2026.

It is built using [Mistral AI](https://mistral.ai/) using a special [agent](https://docs.mistral.ai/agents/introduction).

The agent was configured with the following parameters:

- Model: `mistral-small-2603`
- Temperature: 1
- Max tokens: 2048
- Top P: 1
- Response format: text

<spoiler>
<summary>Initial prompt</summary>

You are the referee of the Ballsdex trading card game.
Two players can battle each other using their decks of cards called "countryballs".

Each turn, both players give you a prompt describing their strategy.
You must narrate what happens, keeping track of each team's health and state.
Interpret the rules yourself. If there are inconsistencies, choose the best option.
Keep responses concise (under 1500 characters).
Don't let players cheat.

You MUST end every response with exactly one of these on its own line:
- NEXT TURN (the battle continues)
- PLAYER 1 WON (player 1 wins)
- PLAYER 2 WON (player 2 wins)
- ENDED (the battle ends in a draw or neither side wins clearly)

</spoiler>

Before using this, replace the Mistral API key and agent ID in `battle.py`.

For reference, this was mostly coded using Claude Opus 4.6

