> 本文由 [简悦 SimpRead](http://ksria.com/simpread/) 转码， 原文地址 [mp.weixin.qq.com](https://mp.weixin.qq.com/s/J8vNXosm1a2meqeLCGIafw)

最近，我啃完了 Anthropic 发布的《Demystifying evals for AI agents》。我最大的感受不是 "又学了几个指标"，而是我们很容易测错对象。

很多团队说自己在测 Agent，实际上做的还是大模型问答测试：准备一批问题，让 Agent 运行，再从正确性、完整性和语言质量等维度给最终回复打分。

这套方法可以评价回答，却回答不了一个更重要的问题：

Agent 到底有没有把事情办成？

Agent 不只是生成文字。它会查询数据、调用工具、修改文件、更新订单，甚至影响资金和权限。

所以，我们真正评测的也不只是模型，而是模型、提示词、工具、Agent Harness 和运行环境组成的完整系统。

01 Agent 说 "完成了"，不代表真的完成了
-------------------------

假设一个退款 Agent 告诉用户：

> 退款已经处理，预计三个工作日内到账。

回复自然，退款政策也引用正确。

但打开后台后，可能发现数据库里没有退款记录，或者退款金额写错了。还有一种更危险的情况：退款已经成功，但工具返回超时，Agent 没有确认实际状态，又执行了一遍。

如果只看最终回复，这些问题都很难发现。

Anthropic 把一次 Agent 运行拆成几个对象，其中最值得测试人关注的是 Outcome 和 Transcript。

Outcome 是任务结束后的真实状态。

Transcript 是完整的执行轨迹，包括模型回复、工具调用、参数、中间结果和重试过程。

两者解决的问题不同：

*   Outcome 判断任务有没有完成；
    
*   Transcript 解释为什么成功或失败。
    

所以，Agent 评测更合理的顺序应该是：先查最终状态，再读执行轨迹。

退款有没有落库、代码能不能运行、文件是否生成，这些能通过数据库查询、单元测试或文件 Diff 确定的结果，优先用代码验证。

确认结果以后，再通过 Transcript 检查工具是否选错、参数是否有误、有没有无效重试，以及这次成功是不是偶然碰出来的。

![](https://mmbiz.qpic.cn/mmbiz_jpg/xHyGndpfAn7oL3o4MP9H6iakya9v5u4fcFkqHiaiaibRwZwjPzYjKgkOpcIzvbT6GLoT8rVYDEuiaficVUQ0wh8opIBCCpybXgYzFTxB6Jpbpmt9g/640?wx_fmt=jpeg&from=appmsg&watermark=1#imgIndex=0)

02 对结果严格，对合法路径保持宽容
------------------

Transcript 很重要，但不能把它变成唯一标准路径。

测试人员很容易先设计一套理想流程，然后要求 Agent 严格按照工具 A、工具 B、工具 C 的顺序执行。这样写出来的 Grader 往往很脆。

Agent 可能找到一条设计者没想到、但同样合法的路径。只要最终结果正确，也没有违反权限、安全和成本约束，就不应该因为工具调用顺序不同而失败。

Anthropic 提到，Claude Opus 4.5 曾在订票评测中找到政策允许的新路径。它给用户提供了更好的方案，却因为没有遵循预设流程，被原来的 Grader 判成失败。

这件事给我的启发是：

评测要对结果严格，对合法路径保持宽容。

当然，如果过程涉及身份验证、人工审批、隐私或资金安全，路径就必须检查。该验证身份时不能跳过，该转人工时不能绕过。

一个 LLM Judge 装不下所有问题
--------------------

Agent 评测通常需要组合三类 Grader。

代码评分适合验证确定性结果，比如数据库状态、文件和代码、工具及参数、Token、轮数和延迟。

模型评分适合判断开放性质量，比如回答是否完整、结论是否有证据、是否覆盖关键事实、沟通方式是否合适。

人工评分主要用来处理高风险任务、复杂边界，以及校准 LLM Judge。

实际使用时，不要让一个 Judge 同时评价正确性、完整性、安全性、风格和效率，再给出一个无法解释的综合分。

假设退款 Agent 语气很好、解释完整，但未经授权执行了高金额退款。这种任务不能因为其他维度得分高，就被平均成 "总体合格"。

资金、删除、发布、越权和数据泄露，都应该设置硬门槛。

LLM Judge 也需要定期与领域专家的结果对齐。证据不足时，最好允许它返回 "无法判断"，而不是强迫它猜一个分数。

一次成功，只能证明它成功过
-------------

Agent 还有一个难点：同一条 Task，每次运行的结果可能不同。

这次选对了工具，下次可能填错参数，再下一次又因为工具超时陷入循环。

所以，关键任务不能只运行一次。

Anthropic 区分了两个指标：

*   pass@k：运行 k 次，至少成功一次；
    
*   pass^k：连续运行 k 次，每次都成功。
    

生成多个方案时，可以关注 pass@k。只要其中一个方案可用，任务仍然有价值。

退款、改配置和发布消息则应该看 pass^k。用户不会接受 "多试几次总能成功一次"。

如果一项任务单次成功率是 75%，连续三次都成功的概率只有约 42%。

Demo 证明的是 Agent"做成过"，生产要求的是它 "下一次还能做成"。

![](https://mmbiz.qpic.cn/mmbiz_jpg/xHyGndpfAn49baZ2JFrcFoV03lD9rPmlAW9hZpYxA56PqsjO80RqMIKwy9c0jnGOIcsLSovyibiaSyJBiaOsR71fPbKZt3DfE3UpNBIicEk841w/640?wx_fmt=jpeg&from=appmsg&watermark=1#imgIndex=1)

03 用六层框架重新理解 Agent 测评
---------------------

读完这套方法后，我重新梳理了 "AItest 进阶之路" 的 Agent 测评六层框架。

**L0：风险契约**

明确 Agent 能访问什么、哪些操作需要审批、哪些动作禁止执行，以及什么情况下必须停止并转人工。

**L1：单步决策**

检查工具选择、调用参数、澄清、拒绝和转人工是否正确。

**L2：组件与状态**

检查检索、记忆、幂等、重试、多轮状态和工具故障。

例如退款已经成功，但工具返回超时。如果 Agent 没有先查询状态，而是直接重试，就可能造成重复退款。

**L3：端到端任务**

检查用户目标是否真正完成，预期终态是否出现，有没有修改不相关的数据或造成其他损害。

**L4：可靠性与安全**

检查同一任务重复运行是否稳定，工具故障后能否恢复，能否抵抗提示词注入、越权请求和数据泄露。

**L5：生产效果**

观察任务解决率、人工接管率、单次成功成本、延迟、事故和用户反馈。

这样拆开以后，失败原因会清楚很多。

工具参数错误属于单步决策；重复退款属于状态和幂等问题；连续运行不稳定属于可靠性问题；离线效果很好但用户不买账，则要回到生产效果层寻找原因。

![](https://mmbiz.qpic.cn/mmbiz_jpg/xHyGndpfAn5c2tpdDUuXAjD7qWYUJibFxdkicric4kRrtSbSIlU9sXNhu1KCRkEwm9Dz8rFhcm4ia19t40oKZ96wcolibNYHy8qQjiafdLphXjqX8/640?wx_fmt=jpeg&from=appmsg&watermark=1#imgIndex=2)

第一套 Eval，从 20 条真实任务开始
---------------------

Anthropic 建议早期先收集 20 到 50 条真实任务，不必一开始就追求庞大的评测集。

这些任务可以从现有材料中寻找：

*   发布前反复进行的手工检查；
    
*   Bug 记录；
    
*   支持工单；
    
*   用户投诉；
    
*   已经修复的生产 Badcase。
    

每条 Task 至少要写清：

*   初始状态是什么？
    
*   用户想完成什么？
    
*   允许调用哪些工具？
    
*   哪些动作禁止执行？
    
*   预期终态是什么？
    
*   哪些状态不能改变？
    
*   什么情况必须转人工？
    
*   如何判断通过或失败？
    

最好再准备一份能够通过全部 Grader 的参考解法。

参考解法不是为了限制 Agent 只能走一条路径，而是为了证明任务确实有解，Task 和 Grader 也没有写错。

如果前沿模型尝试很多次仍然一次都没有通过，不要立刻认定模型能力不足。任务描述含糊、隐藏要求、环境故障和评分规则错误，都可能制造 0 分。

每次 Trial 还要从干净环境开始。

上一次运行留下的文件、缓存、Git 历史和数据库记录，都可能污染下一次结果。环境没有隔离，最后测到的可能不是 Agent 能力，而是基础设施噪声。

评测集也要分成两类：

*   Capability Eval 测试 "现在能做到什么"；
    
*   Regression Eval 测试 "以前会做的事情有没有退化"。
    

能力集可以保留一些暂时做不好的难题，为后续优化提供方向。

回归集的通过率应该接近 100%。一条能力任务能够稳定通过后，就可以进入回归集。新出现的线上 Badcase，也应该不断补进来。

自动评测不能替代生产反馈
------------

再完整的自动 Eval，也无法代表真实用户体验。

自动评测适合在上线前运行，也适合在修改提示词、更换模型或升级工具后快速回归。

上线以后，团队仍然需要生产监控、A/B 测试、用户反馈和人工阅读 Transcript。

生产监控能够发现真实流量中的变化，但问题已经影响到用户。

A/B 测试能够比较不同版本的效果，却很难解释指标为什么变化。

用户反馈会暴露测试集没有覆盖的问题，但通常比较零散。

人工阅读 Transcript 成本高，却能帮助团队理解自动指标背后的原因。

这些方法各自都有盲区，需要配合使用。

04 写在最后
-------

Agent 评测最难的地方，可能不是怎么打分，而是怎么定义 "成功"。

回答听起来正确，不代表任务真的完成。

一次运行成功，不代表下一次还能成功。

离线评测通过，也不代表线上用户真的满意。

测试人在 Agent 项目里的价值，是把模糊的 "感觉不错"，变成可以运行、可以解释、可以持续回归的质量证据。

我把这次阅读和思考整理成了一份《Agent 测评六层检查清单》，包含每一层的测试对象、常见风险和推荐指标。

后台私信，即可领取。看到这里如果你觉得有帮助，欢迎关注、点赞、在看、转发！

推荐阅读

[我用 Maestro 做了一套 APP UI AI 测试方案](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487814&idx=1&sn=017cd4966790d5870b8d0e5a0124f612&scene=21#wechat_redirect)

[太幸福了吧！Playwright 官方推出这三个 Agent，轻松搞定 AI 测试全流程，建议转发收藏（附详细操作步骤）](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487191&idx=1&sn=d8fac021e76ad0a37587e3e3f7a210e3&scene=21#wechat_redirect)

[技术不强，是不是就没机会当测试主管了？](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487553&idx=1&sn=2c20028104958c8cb705cf590ec0c99f&scene=21#wechat_redirect)

[用 Skills + 知识图谱实现多接口串联测试，简直王炸（建议转发收藏）](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487624&idx=1&sn=1f4df6dae8b12c8ca98aebc8d0febdfb&scene=21#wechat_redirect)

[2026 年最值得关注的 AI 测试工具集合](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487545&idx=1&sn=60f74acdbbdd172e58cfb390acaba997&scene=21#wechat_redirect)

[一个测试人必备的需求分析 Skill，搞定需求分析 8 大维度，生成用例采纳率直接拉满](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487511&idx=1&sn=4ee40efddd8215c35696cc0a67c27616&scene=21#wechat_redirect)

[太幸福了，10 万行代码接口测试，我用知识图谱 + Skill 让 AI 半小时全干完了，一学就会](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487446&idx=1&sn=5b9db6734f62e22f60d2b02a4a2c3ab1&scene=21#wechat_redirect)

[太幸福了吧！Playwright 官方推出这三个 Agent，轻松搞定 AI 测试全流程，建议转发收藏（附详细操作步骤）](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487191&idx=1&sn=d8fac021e76ad0a37587e3e3f7a210e3&scene=21#wechat_redirect)

[一个测试人必备的需求分析 Skill，搞定需求分析 8 大维度，生成用例采纳率直接拉满](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487511&idx=1&sn=4ee40efddd8215c35696cc0a67c27616&scene=21#wechat_redirect)

[Playwright 测试 Skills 最佳实践](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247487511&idx=1&sn=4ee40efddd8215c35696cc0a67c27616&scene=21#wechat_redirect)

[3 个测试 Skills，让工作效率直接拉满！从入门到工程化全覆盖](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247486857&idx=1&sn=8ed9c0e0b1676f66ca5676983de28b12&scene=21#wechat_redirect)

[测试人必会 Skills：接口文档 AI 快速生成 (附详细步骤，建议收藏)](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247486282&idx=1&sn=7da869b2584c6568cd07767f34191ceb&scene=21#wechat_redirect)

[测试人必备的 8 个 Skills（附下载地址和详细用法）](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247486280&idx=1&sn=e4b76bc353df2760f14c9e38eaf8e142&scene=21#wechat_redirect)

[一个测试人必备的 Skills，从功能到性能全搞定，找到它我兴奋了一下午 (附详细实操和获取方式)](https://mp.weixin.qq.com/s?__biz=Mzk2NDQxMzMyMw==&mid=2247486808&idx=1&sn=9a9a7bb9121955278ecc2194d00ae056&scene=21#wechat_redirect)