# 在 2 核 Ubuntu 上运行群聊模型

按 Ubuntu 24.04 x64、2 vCPU、16 GB 内存、20 GB 系统盘、无独立 GPU 估算。推荐用 **Qwen/Qwen3.5-2B 对话模型**训练中文接话风格，部署其 **Q4_K_M 文本 GGUF**。公开量化版约 1.28 GB，可在租卡前用它测试同尺寸模型。默认启用非思考模式。[Qwen 模型卡](https://huggingface.co/Qwen/Qwen3.5-2B) · [量化文件](https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/blob/main/Qwen3.5-2B-Q4_K_M.gguf)

| 可选模型 | Q4_K_M 权重文件 | 在这台主机上的用途 |
| --- | ---: | --- |
| Qwen3.5-0.8B | [0.53 GB](https://huggingface.co/unsloth/Qwen3.5-0.8B-GGUF/blob/main/Qwen3.5-0.8B-Q4_K_M.gguf) | 对延迟很敏感、对话任务简单时的备选 |
| Qwen3.5-2B | 1.28 GB | 首选，用于单群或几个低频群的中文短回复 |
| Qwen3.5-4B | [2.74 GB](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/blob/main/Qwen3.5-4B-Q4_K_M.gguf) | 质量不足时的下一档，先用相同提示实测等待时间 |
| Qwen3-8B | [5.03 GB](https://huggingface.co/Qwen/Qwen3-8B-GGUF/blob/main/Qwen3-8B-Q4_K_M.gguf) | 管理页可直接与 2B 切换试聊，适合低频对比回复质量 |
| Qwen3.5-9B | [5.68 GB](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/blob/main/Qwen3.5-9B-Q4_K_M.gguf) | 内存有机会装下，双核短时多条消息容易排队 |
| Qwen3-14B | [9.00 GB](https://huggingface.co/Qwen/Qwen3-14B-GGUF/blob/main/Qwen3-14B-Q4_K_M.gguf) | 短上下文、主机空闲时可尝试加载，和 AstrBot 共机时余量很小 |

权重之外还要容纳上下文缓存、计算缓冲区、Ubuntu、AstrBot 和消息适配器。此次 CPU 镜像大小约 0.82 GB。若想同时留两份 2B 量化权重用于更新和回退，建议在系统与机器人现有占用之外预留约 5 GB 磁盘，语料另计。先用 `free -h`、`df -h` 查看实际余量。

## 用公开模型验证延迟

在 **插件详情 → 人格管理 → 模拟对话** 可以直接比较 2B 和 8B。通过 Git 更新插件后，分别选一档，发同一句群聊消息即可。首次发送会下载对应权重，回答结束会释放 CPU 引擎，下一轮的等待时间包含模型加载。两档同时缓存时，插件数据盘建议预留约 6.5 GB。

在开发机的 Linux 容器中限制 2 vCPU、12 GiB 内存，用同一句中文短消息各测一次。权重已下载，放在 Windows 宿主机挂载的目录里。从提交到收到完整短答，2B 用了 27 秒，8B 用了 97 秒，包含首次文件校验、模型加载和生成。采样到的推理进程峰值驻留内存分别是 1.92 GiB 和 8.44 GiB。云服务器的 CPU 和磁盘会直接影响等待时间。

### 为群聊启动常驻模型服务

在安装 Docker Engine 和 Compose v2 的 Ubuntu 上，从本插件目录执行。命令将基线模型存放在插件目录外，先校验 SHA256 再启用文件。

```bash
mkdir -p "$HOME/models/persona"
export PERSONA_GGUF="$HOME/models/persona/Qwen3.5-2B-Q4_K_M.gguf"
curl -fL -C - -o "$PERSONA_GGUF.part" \
  'https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/Qwen3.5-2B-Q4_K_M.gguf' &&
  printf '%s  %s\n' aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223 "$PERSONA_GGUF.part" | sha256sum -c - &&
  mv -- "$PERSONA_GGUF.part" "$PERSONA_GGUF"
docker compose -f deploy/compose.local-model.yaml up -d --wait --wait-timeout 180
python3 deploy/check_chat.py
python3 deploy/check_chat.py --system-file persona_seed.txt
```

首次启动会拉取固定版本的 llama.cpp CPU 镜像。服务使用 2 个推理线程、4096 tokens 总窗口、1 个并发槽和默认 128 tokens 输出。测速脚本发一条虚构群消息，分别显示第一段正文出现时间、整条回复时间和引擎报告的生成速度。第二条命令会带上完整推荐人格，可观察长提示对首段延迟的影响。在试用群还要计入实际聊天历史、工具定义和同时到达的请求。[llama.cpp 服务参数](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

2026-11-25 在 i5-14600K 开发机的 Linux 容器中限制为 2 vCPU，各测一次，模型已加载。短提示 47 个输入 tokens，0.60 秒出现正文，1.69 秒完成 25 个输出 tokens。推荐人格加同一条消息共 1067 个输入 tokens，9.23 秒出现正文，11.37 秒完成 46 个输出 tokens。两次请求期间容器 cgroup 峰值约 2.05 GiB。云服务器的 CPU 型号、共享核争用及内存带宽会影响结果。

首段等待过长时，可先缩短核心提示和最近消息、精简试用群的人格工具列表。若实际输入连同预留输出超过 4096，可通过 `PERSONA_CONTEXT` 调整窗口，重新执行 Compose 启动命令，并同步更新 AstrBot 中该模型的上下文设置。

## 让一个 QQ 群试用

1. 在 AstrBot 的模型提供商页面新增 OpenAI 兼容的聊天提供商。AstrBot 直接运行在主机或使用主机网络时，API Base 填 `http://127.0.0.1:18080/v1`。模型名填 `persona-chat`，启用文本输入，模型上下文填 4096，单次输出先设 96～128 tokens。若客户端要求填写密钥，可填占位值 `local`。端口仅发布到宿主机回环地址。[AstrBot 本地模型接入](https://docs.astrbot.app/providers/provider-lmstudio.html)
2. **AstrBot 在普通 Docker 容器中时**，先让 AstrBot 加入模型服务所在网络。例如容器名为 `astrbot` 时执行 `docker network connect persona-local_default astrbot`，API Base 改填 `http://persona-model:8080/v1`。如需在重建 AstrBot 容器后继续使用，请把 `persona-local_default` 作为外部网络加入现有 AstrBot Compose 服务，保留它原有的网络。
3. 在本插件的“设置与方向 → 模型与预算”选择刚启用的**群聊本地模型**。在“群与共享”勾选一个测试群的**文字群聊使用本地模型**，保存。在群里正常唤醒可可子，检查 AstrBot 调用记录中的提供商。清空模型字段或取消该群勾选即可恢复会话原有选择。
4. 本地选择只作用于门禁放行、命中目标人格的一次普通文字对话，可以带 @ 和已展开的纯文字引用。私聊、媒体、内容未知的引用、命令和显式指定模型或请求的其他插件走原路由。AstrBot 配置的调用失败回退仍由框架执行。如需测试本地服务断线后的行为，请同时检查试用会话的回退提供商。

后台记忆整理沿用“后台学习模型”。留空时从 AstrBot 读取该会话的常规模型。本地群聊开关只覆盖单次前台请求。建议让长批次 JSON 整理继续用已有模型，给两核 CPU 留出及时接话的余量。

## 用群聊记录训练

- **训练机**建议租一张 RTX 4090 24 GB，配 32 GB 主机内存及约 50 GB 可用工作盘。24 GB 是带有调参余量的一档。使用原始 Qwen3.5-2B 对话权重做 BF16 LoRA，只训练语言层。第一轮可从 rank 16、序列长度 1024、micro-batch 2、梯度累积 8、1 个 epoch 起步。随着验证集表现调整。训练与导出都放在租用机器上。[Unsloth 微调与导出指南](https://unsloth.ai/docs/models/qwen3.5/fine-tune)
- **训练样本**按“同一段对话的上文 → 希望可可子给出的回复”配对。可从已获准用于训练的群聊导出开始。工作区的“对话语料库”插件导出包内含 `messages.jsonl`，可用回复目标编号定位上下文。协议端可能漏收机器人自己的发言，缺失的目标回复可人工补写。先删去 QQ 号、联系方式和敏感原话，再把脱敏后的训练样本送到租机。只对目标回复计算损失。[TRL 对话式监督微调](https://huggingface.co/docs/trl/sft_trainer)
- **验证**按完整会话和时间段切分，留最后约 10% 的会话。用同一组未见过的多轮群聊盲评原版与微调版，观察是否接对人、接得自然、是否复读或编造群友经历。会变化的群友近况继续由现有记忆库提供。
- **上线**时在租机合并适配器和相同版本的原始权重，保留训练时的 tokenizer、聊天模板和结束标记，导出 Q4_K_M 文本 GGUF。在同一批留出对话上复测导出文件。把单个成品传到服务器的模型目录，令 `PERSONA_GGUF` 指向其绝对路径，再运行上面的 Compose 启动和测速命令。服务模型名仍为 `persona-chat`。

下面是一条虚构的训练样本。每行保存一个这样的 JSON 对象，训练器可只对 `completion` 中的可可子回复计算损失。

```json
{"prompt":[{"role":"system","content":"你是可可子，用一两句中文接话。"},{"role":"user","content":"群友甲：昨天卡在最后两拍，今天终于全连了！"}],"completion":[{"role":"assistant","content":"最后两拍也拿下了！今天是换了手法，还是练着练着就顺了？"}]}
```

模型更新建议使用新的文件名，以便对比和切回。Compose 的启动和停止命令需要同一个 `PERSONA_GGUF` 环境变量。停止服务使用 `docker compose -f deploy/compose.local-model.yaml down`。
