# Model Context Protocol (MCP) Integration

## ⛔ Not available on eeroNotebook at v1 — do not use the configuration below

> **The instructions on this page still work, and that is the problem.** They tell you to put `OPEN_NOTEBOOK_PASSWORD` in an MCP client's config file. On eeroNotebook that value is the **operator's** credential: the MCP server authenticates as the admin member, which owns every Notebook that existed before member accounts did. Following this page hands a third-party MCP client — and every AI-assistant conversation through it — read and write access to the entire library, plus the instance's model and processing settings.
>
> **Access enforcement is not the gap.** The MCP server holds no database access; every tool it exposes is an ordinary HTTP request through this API's authentication middleware and per-route ownership checks. A member with no access gets an empty list from `list_notebooks`, a 404 from every read and write addressed by id, and no results from `search`. That was measured against the published package, not assumed. Requirement 7.2 ("the MCP interface enforces the same access rules as the REST API") is therefore satisfied by there being exactly **one** enforcement point, in this API — there is no second one to look for, and no MCP server in this repository.
>
> **What is missing is a credential an MCP client can hold.** A member's access token does work in the `OPEN_NOTEBOOK_PASSWORD` slot and is correctly scoped, but it expires after one hour and the MCP server cannot refresh it: every tool then fails until someone pastes a new token and restarts the client. The operator's password is the only credential that does not expire, and giving it to an MCP client is the thing described above.
>
> Enabling this later needs a long-lived, revocable member token issued by this API, or refresh support in the MCP server (a [separate repository](https://github.com/Epochal-dev/open-notebook-mcp)). See [ADR-010](../7-DEVELOPMENT/decisions/ADR-010-mcp-not-offered-at-v1.md) for the full reasoning, what was measured, and what enabling it would take.
>
> Everything below is upstream Open Notebook's documentation, kept for reference and for whoever revisits this.

---

Open Notebook can be seamlessly integrated into your AI workflows using the **Model Context Protocol (MCP)**, enabling direct access to your notebooks, sources, and chat functionality from AI assistants like Claude Desktop and VS Code extensions.

## What is MCP?

The [Model Context Protocol](https://modelcontextprotocol.io) is an open standard that allows AI applications to securely connect to external data sources and tools. With the Open Notebook MCP server, you can:

- 📚 **Access your notebooks** directly from Claude Desktop or VS Code
- 🔍 **Search your research content** without leaving your AI assistant
- 💬 **Create and manage chat sessions** with your research as context
- 📝 **Generate notes** and insights on-the-fly
- 🤖 **Automate workflows** using the full Open Notebook API

## Quick Setup

### For Claude Desktop

1. **Install the MCP server** (automatically from PyPI):

   ```bash
   # No manual installation needed! Claude Desktop will use uvx to run it automatically
   ```

2. **Configure Claude Desktop**:

   **macOS/Linux**: Edit `~/Library/Application Support/Claude/claude_desktop_config.json`

   ```json
   {
     "mcpServers": {
       "open-notebook": {
         "command": "uvx",
         "args": ["open-notebook-mcp"],
         "env": {
           "OPEN_NOTEBOOK_URL": "http://localhost:5055",
           "OPEN_NOTEBOOK_PASSWORD": "your_password_here"
         }
       }
     }
   }
   ```

   **Windows**: Edit `%APPDATA%\Claude\claude_desktop_config.json`

   ```json
   {
     "mcpServers": {
       "open-notebook": {
         "command": "uvx",
         "args": ["open-notebook-mcp"],
         "env": {
           "OPEN_NOTEBOOK_URL": "http://localhost:5055",
           "OPEN_NOTEBOOK_PASSWORD": "your_password_here"
         }
       }
     }
   }
   ```

3. **Restart Claude Desktop** and start using your notebooks in conversations!

### For VS Code (Cline and other MCP-compatible extensions)

Add to your VS Code settings or `.vscode/mcp.json`:

```json
{
  "servers": {
    "open-notebook": {
      "command": "uvx",
      "args": ["open-notebook-mcp"],
      "env": {
        "OPEN_NOTEBOOK_URL": "http://localhost:5055",
        "OPEN_NOTEBOOK_PASSWORD": "your_password_here"
      }
    }
  }
}
```

## Configuration

- **OPEN_NOTEBOOK_URL**: URL to your Open Notebook API (default: `http://localhost:5055`)
- **OPEN_NOTEBOOK_PASSWORD**: Optional - only needed if you've enabled password protection

> **On eeroNotebook, neither line is accurate.** Authentication is never optional: every request resolves to a member or is refused. And this variable is not a password field — the MCP server copies its value straight into an `Authorization: Bearer` header, so it holds whichever credential you give it. Setting it to the operator's password signs the MCP client in *as the operator*. See the notice at the top of this page and [ADR-010](../7-DEVELOPMENT/decisions/ADR-010-mcp-not-offered-at-v1.md).

### For Remote Servers

If your Open Notebook instance is running on a remote server, update the URL accordingly:

```json
"OPEN_NOTEBOOK_URL": "http://192.168.1.100:5055"
```

Or with a domain:

```json
"OPEN_NOTEBOOK_URL": "https://notebook.yourdomain.com/api"
```

## What You Can Do

Once connected, you can ask Claude or your AI assistant to:

- _"Search my research notebooks for information about [topic]"_
- _"Create a new note summarizing the key points from our conversation"_
- _"List all my notebooks"_
- _"Start a chat session about [specific source or topic]"_
- _"What sources do I have in my [notebook name] notebook?"_
- _"Add this PDF to my research notebook"_
- _"Show me all notes in [notebook name]"_

The MCP server provides full access to Open Notebook's capabilities, allowing you to manage your research seamlessly from within your AI assistant.

## Available Tools

The Open Notebook MCP server exposes these capabilities:

### Notebooks

- List notebooks
- Get notebook details
- Create new notebooks
- Update notebook information
- Delete notebooks

### Sources

- List sources in a notebook
- Get source details
- Add new sources (links, files, text)
- Update source metadata
- Delete sources

### Notes

- List notes in a notebook
- Get note details
- Create new notes
- Update notes
- Delete notes

### Chat

- Create chat sessions
- Send messages to chat sessions
- Get chat history
- List chat sessions

### Search

- Vector search across content
- Text search across content
- Filter by notebook

### Models

- List configured AI models
- Get model details
- Create model configurations
- Update model settings

### Settings

- Get application settings
- Update settings

## MCP Server Repository

The Open Notebook MCP server is developed and maintained by the Epochal team:

**🔗 GitHub**: [Epochal-dev/open-notebook-mcp](https://github.com/Epochal-dev/open-notebook-mcp)

Contributions, issues, and feature requests are welcome!

## Finding the Server

The Open Notebook MCP server is published to the official MCP Registry:

- **Registry**: Search for "open-notebook" at [registry.modelcontextprotocol.io](https://registry.modelcontextprotocol.io)
- **PyPI**: [pypi.org/project/open-notebook-mcp](https://pypi.org/project/open-notebook-mcp)
- **GitHub**: [Epochal-dev/open-notebook-mcp](https://github.com/Epochal-dev/open-notebook-mcp)

## Troubleshooting

### Connection Errors

1. Verify the `OPEN_NOTEBOOK_URL` is correct and accessible
2. If using password protection, ensure `OPEN_NOTEBOOK_PASSWORD` is set correctly
3. For remote servers, make sure port 5055 is accessible from your machine
4. Check firewall settings if connecting to a remote server

## Using with Other MCP Clients

The Open Notebook MCP server follows the standard MCP protocol and can be used with any MCP-compatible client. Check your client's documentation for configuration details.

## Learn More

- [Model Context Protocol Documentation](https://modelcontextprotocol.io)
