# govmem demo

Two people, each with their own agent, over one governed shared memory. Everything runs through the real
library: the interpreter reads each shared message, the core authorises and applies the operations, the
organiser annotates and links, the gate decides what each agent may see, and the agent answers only from what
was released. The three example conversations in `scenarios/` are just lists of messages; Play sends them one
by one through the same endpoints as the input boxes.

```bash
cd govmem
source scripts/env_soclaas.sh                       # memory model and agent on SoCLaaS qwen3.8:27b
~/miniconda3/envs/gsm/bin/python -m uvicorn demo.server:app --host 127.0.0.1 --port 8765
```

Open http://localhost:8765 (from VS Code Remote-SSH, forward port 8765). A shared message takes 15 to 50 seconds
(two to four model calls); a question takes a few seconds. `GOVMEM_DEMO_THINKING=1` turns on the agent's
reasoning (slower). Add a conversation by dropping a JSON file with `principals`, optional `entities` and
`relationships`, and `steps` (`speaker`, `action` = share | ask, `text`) into `scenarios/`.
