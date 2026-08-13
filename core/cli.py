from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.history import InMemoryHistory

from core.chat import Chat


class CliApp:
    def __init__(self, agent: Chat):
        self.agent = agent

        self.history = InMemoryHistory()
        self.session = PromptSession(
            history=self.history,
            style=Style.from_dict({"prompt": "#aaaaaa"}),
        )

    async def run(self):
        while True:
            try:
                user_input = await self.session.prompt_async("> ")
                if not user_input.strip():
                    continue

                text = user_input
                thinking = False
                if text.startswith("/think "):
                    text = text[len("/think "):]
                    thinking = True

                response = await self.agent.run(text, thinking=thinking)
                print(f"\nResponse:\n{response}")

            except KeyboardInterrupt:
                break
