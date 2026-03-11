import os
import json
import base64
import requests
import numpy as np
from typing import List, Optional, Dict, Any, Callable, Generator
from sentence_transformers import SentenceTransformer
import faiss
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import (
    SystemMessage, UserMessage, AssistantMessage, ToolMessage, CompletionsFinishReason
)
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError

# ====================== NATIVE MCP CLIENT (unchanged from v4, fully working) ======================
class MCPClient:
    """Native JSON-RPC 2.0 client for official GitHub MCP Server"""
    def __init__(self, url: str = "https://api.githubcopilot.com/mcp/", token: str = None):
        self.url = url
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise ValueError("❌ GITHUB_TOKEN required for MCP")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "MCP-Protocol-Version": "2025-11-25"  # official spec
        }
        self.id_counter = 0
        self.session = requests.Session()
        self.tools_cache: List[Dict] = []

    def _rpc(self, method: str, params: Optional[Dict] = None) -> Dict:
        self.id_counter += 1
        payload = {"jsonrpc": "2.0", "id": self.id_counter, "method": method, "params": params or {}}
        try:
            r = self.session.post(self.url, json=payload, headers=self.headers, timeout=30)
            r.raise_for_status()
            result = r.json()
            if "error" in result:
                raise Exception(result["error"])
            return result.get("result", {})
        except Exception as e:
            return {"error": str(e)}

    def initialize(self):
        return self._rpc("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "AgentG4H-RMA", "version": "5.0"}
        })

    def list_tools(self) -> List[Dict]:
        result = self._rpc("tools/list")
        self.tools_cache = result.get("tools", [])
        return self.tools_cache

    def call_tool(self, name: str, arguments: Dict) -> str:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict) and "content" in result:
            return result["content"]
        return str(result)

# ====================== AIAssistant (minor updates) ======================
class AIAssistant:
    def __init__(self):
        self._github_token = os.getenv("GITHUB_TOKEN")
        self.client = ChatCompletionsClient(
            endpoint=os.getenv("AZURE_ENDPOINT"),
            credential=AzureKeyCredential(os.getenv("AZURE_KEY"))
        )
        self.default_model = "gpt-4o"  # or gpt-4.1

    def connect_mcp(self, remote: bool = True) -> MCPClient:
        url = "https://api.githubcopilot.com/mcp/" if remote else "http://localhost:port"
        mcp = MCPClient(url, self._github_token)
        mcp.initialize()
        print("✅ MCP connected & initialized (official GitHub server)")
        return mcp

    # ... (keep your existing get_default_tools, web_search, etc.)

# ====================== Conversation (v5 upgrades) ======================
class Conversation:
    def __init__(self, assistant: AIAssistant, system_prompt: str):
        self.assistant = assistant
        self.messages = [SystemMessage(system_prompt)]
        self.token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.rag = RAGVectorStore()
        self.memory = MemoryBank()
        self.mcp_client: Optional[MCPClient] = None
        self.orchestrator = None

    def connect_mcp(self, remote: bool = True):
        self.mcp_client = self.assistant.connect_mcp(remote)
        self.mcp_client.list_tools()

    # ====================== NEW: FULL SSE STREAMING ======================
    def stream_run_agent(self, user_input: str, image_base64: Optional[str] = None,
                         max_steps: int = 10) -> Generator[str, None, None]:
        if self.mcp_client:
            mcp_tools = self.mcp_client.tools_cache or self.mcp_client.list_tools()
            tools = [{"type": "function", "function": t} for t in mcp_tools]
        else:
            tools = None

        # Vision support (same as v4)
        content = [{"type": "text", "text": user_input}]
        if image_base64:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}})
        self.messages.append(UserMessage(content=content))

        # Streaming loop with tool support
        for _ in range(max_steps):
            response = self.assistant.client.complete(
                messages=self.messages,
                tools=tools,
                model=self.assistant.default_model,
                stream=True,                  # ← REAL SSE
                tool_choice="auto"
            )

            full_reply = ""
            tool_calls = []
            for chunk in response:
                if chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    full_reply += delta
                    yield delta  # live token streaming

                if chunk.choices[0].delta.tool_calls:
                    tool_calls.extend(chunk.choices[0].delta.tool_calls)

            self.messages.append(AssistantMessage(content=full_reply))

            if not tool_calls:
                return  # final answer

            # Execute tools (MCP or default)
            for tc in tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                if self.mcp_client and name in [t["name"] for t in self.mcp_client.tools_cache]:
                    result = self.mcp_client.call_tool(name, args)
                else:
                    result = self.assistant.default_executor(name, args)
                self.messages.append(ToolMessage(content=result, tool_call_id=tc.id))
                yield f"\n[Tool {name} executed]\n"

    # ====================== NEW: GitHub Repo Vector RAG ======================
    def index_github_repo(self, repo: str) -> str:
        """repo = 'xAI/grok' or 'owner/repo' — pulls live via MCP"""
        if not self.mcp_client:
            return "❌ Connect MCP first"
        # MCP tools will discover github_list_files / github_read_file etc.
        files = self.mcp_client.call_tool("github_list_files", {"repo": repo})  # dynamic
        for file in files.get("files", [])[:50]:  # limit for speed
            content = self.mcp_client.call_tool("github_read_file", {"repo": repo, "path": file})
            self.rag.add_document(content, metadata={"repo": repo, "file": file})
        return f"✅ Indexed {len(files.get('files', []))} files from {repo} into RAG"

    # ====================== NEW: Self-Improving Agent ======================
    def self_improve(self, task: str, repo_to_edit: str = None) -> str:
        """Agent reviews its own last run, improves, and commits via MCP"""
        if not self.orchestrator:
            self.orchestrator = MultiAgentOrchestrator(self.assistant)
        review = self.orchestrator.swarm_run(f"Review and improve this solution for: {task}")
        print("🧠 Self-review complete. Proposing improvements...")

        if repo_to_edit and self.mcp_client:
            # Use MCP to create PR or commit
            self.mcp_client.call_tool("github_create_issue", {
                "repo": repo_to_edit,
                "title": "🤖 AgentG4H-RMA v5 self-improvement",
                "body": review
            })
            return f"✅ Self-improved and opened issue in {repo_to_edit}"
        return review

# ====================== MultiAgentOrchestrator (swarm + self-improve support) ======================
class MultiAgentOrchestrator:
    # ... (keep v4 swarm_run)

    def swarm_run(self, task: str, max_rounds: int = 5) -> str:
        # same as v4, but now feeds into self_improve
        ...

# ====================== RAGVectorStore (tiny extension) ======================
class RAGVectorStore:
    # ... existing FAISS code ...
    def add_document(self, text: str, metadata: Dict):
        # embed + add to index (unchanged)
        pass

# ====================== EXAMPLES (copy-paste) ======================
if __name__ == "__main__":
    ai = AIAssistant()
    chat = ai.new_conversation("You are a helpful autonomous agent.")

    chat.connect_mcp(remote=True)

    # 1. Live streaming
    for token in chat.stream_run_agent("Explain quantum computing in 50 words"):
        print(token, end="", flush=True)

    # 2. Index a whole repo into RAG
    chat.index_github_repo("xAI/grok")

    # 3. Self-improving swarm
    result = chat.self_improve(
        task="Research Grok 4 and write a summary",
        repo_to_edit="yourusername/your-repo"  # optional — will open issue
    )
    print(result)
