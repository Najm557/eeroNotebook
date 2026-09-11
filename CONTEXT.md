# eeroNotebook

Vocabulary for eeroNotebook, a self-hosted study notebook service the team runs on its own internal infrastructure.

## Vocabulary

**eeroNotebook**
: The team's own study and research workspace, in which answers are grounded only in sources the team has supplied. It descends from the upstream Open Notebook project as a maintained code fork, not as an unmodified deployment of it.

_Avoid:_ Open Notebook (the upstream project), NotebookLM (Google's product)

**Dev Server**
: The single shared internal host on which the team's project stacks run alongside one another, each an isolated Docker Compose project joined to one common monitoring network.

_Avoid:_ temporary dev server, the dev box

**Host Ollama**
: The Ollama installation running directly on the Dev Server's operating system, outside any container, which serves local language model inference to every stack that needs it.

_Avoid:_ the AI endpoint, the earoVoice API, the llm-server container

**Notebook owner**
: The team member who created a notebook and holds the sole right to change its sources, notes, and settings. Ownership is distinct from access — granting someone access never confers the ability to edit.

**Share**
: A revocable grant of access to a single notebook for a single named team member. Every share is explicit, so no notebook is visible to anyone but its owner by default, and withdrawing a share ends that member's access without altering the notebook.

_Avoid:_ publish, team library

**Viewer**
: A team member who has been granted access to a notebook they do not own. A viewer may read the sources and ask grounded questions, but cannot alter the notebook's contents.

_Avoid:_ student, collaborator, reader

**Inference Gateway**
: The containerised endpoint through which eeroNotebook obtains all model inference. It presents one stable interface to the application and forwards to whichever backend currently serves the models, so the application holds no knowledge of where inference physically runs.

_Avoid:_ the model server, the LLM proxy
