"""Generate and run a reproducible LumenMind-pi benchmark.

The generated data is template-based synthetic evaluation data.  It is useful
for regression and coverage testing, but must not be described as a human
annotated production benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import httpx


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = ROOT / "data" / "eval" / "benchmark"
INTENT_PATH = BENCHMARK_DIR / "intent_synthetic_v1.jsonl"
RAG_RETRIEVAL_PATH = BENCHMARK_DIR / "rag_retrieval_v2.jsonl"
RAG_ANSWER_PATH = BENCHMARK_DIR / "rag_answer_v1.jsonl"
REPORT_PATH = BENCHMARK_DIR / "benchmark_report.json"
PREDICTIONS_PATH = BENCHMARK_DIR / "intent_predictions.jsonl"


INTENT_BASES: Dict[str, List[str]] = {
    "query": [
        "会员等级在哪里查看", "积分什么时候过期", "标准配送需要多长时间", "这个功能在哪里设置",
        "我想了解服务范围", "怎样查看帮助中心", "有哪些会员权益", "服务时间是什么时候",
    ],
    "complaint": [
        "你们服务太差了", "等了很久还是没人解决", "这次体验让我非常不满意", "处理效率实在太低",
        "我要投诉这次服务", "客服一直没有回应", "这个处理结果太糟糕", "你们这样处理很不合理",
    ],
    "request": [
        "帮我取消这张订单", "请协助修改收货地址", "麻烦处理一下这个需求", "帮我更新联系信息",
        "请帮忙提交变更", "我需要你们协助处理", "帮我调整订单信息", "请替我修改一下资料",
    ],
    "greeting": [
        "你好", "您好", "嗨", "早上好", "晚上好", "hello", "hi", "很高兴见到你",
    ],
    "escalation": [
        "我要找经理", "请让主管来处理", "这个事情需要升级给负责人", "我要联系更高级别客服",
        "请交给管理人员", "让你们负责人跟进", "我要向上级反映", "请升级给主管部门",
    ],
    "technical": [
        "SDK调用总是超时", "接口出现异常但没有错误码", "API连接不上", "系统响应特别慢",
        "回调一直收不到", "服务连接经常中断", "接口参数配置后仍不可用", "程序运行时出现未知错误",
    ],
    "billing": [
        "我想核对本月账单", "订阅费用怎么计算", "账单金额看起来不对", "会员续费价格是多少",
        "想了解收费明细", "这个月的费用如何组成", "在哪里查看消费账单", "订阅套餐如何计费",
    ],
    "account": [
        "我想修改绑定邮箱", "怎样注销账户", "帮我更新账户资料", "我要更换联系邮箱",
        "账户昵称在哪里修改", "如何变更个人资料", "我想关闭当前账号", "绑定信息需要更新",
    ],
    "feedback": [
        "你们服务很好", "这次处理得很棒", "我对结果很满意", "谢谢客服的帮助",
        "整体体验不错", "这个功能很好用", "客服回复很及时", "给你们一个好评",
    ],
    "order_status": [
        "订单处理到哪一步了", "我的商品发出来了吗", "想查询订单当前状态", "这张订单还在处理中吗",
        "订单什么时候开始处理", "帮我看看订单进度", "购买记录显示什么状态", "订单是否已经完成",
    ],
    "logistics": [
        "快递走到哪里了", "包裹什么时候送到", "物流信息一直没更新", "我的货还没收到",
        "运单现在到哪个城市了", "配送为什么这么慢", "快递预计哪天到", "包裹运输进度如何",
    ],
    "refund": [
        "我想申请退款", "钱什么时候退回来", "这次购买可以退掉吗", "退款多久到账",
        "审核通过后多久能收到钱", "我要把商品退回去", "这笔款项怎么退", "不想要了能把钱退回吗",
    ],
    "invoice": [
        "帮我开发票", "电子发票在哪里下载", "发票抬头怎么填写", "公司税号填错了",
        "购买后如何获取票据", "我要补开一张发票", "开票信息需要修改", "什么时候能收到电子票",
    ],
    "payment_issue": [
        "银行卡被扣了两次", "付款一直失败", "为什么多扣了一笔钱", "支付后显示没有成功",
        "同一订单重复扣费", "钱扣了但订单仍未支付", "付款页面提示交易失败", "扣款金额和订单不一致",
    ],
    "account_security": [
        "发现账户有异常登录", "我的账号可能被盗了", "怎样重置密码保护账户", "有人在别的设备登录",
        "账户出现不认识的操作", "如何开启两步验证", "怀疑密码已经泄露", "账号安全需要检查",
    ],
    "technical_login": [
        "一直无法登录", "登录时报401", "收不到登录验证码", "输入密码后进不去",
        "账号登录总是失败", "认证凭证好像过期了", "登录页面一直拒绝访问", "重新登录仍然没有反应",
    ],
    "technical_crash": [
        "应用一打开就崩溃", "页面打开后立刻闪退", "接口返回500错误", "程序运行几秒就退出",
        "客户端突然崩掉", "点击功能后页面直接关闭", "服务端出现内部错误", "应用启动后无法保持运行",
    ],
    "human_handoff": [
        "请转人工", "我想找人工客服", "这个问题需要人工处理", "让真人客服联系我",
        "不要机器人回答了", "帮我接人工服务", "我要和客服人员沟通", "请安排人工继续处理",
    ],
    "other": [
        "这个情况有点奇怪", "我不太清楚怎么描述", "有件事情想说一下", "现在感觉不太对",
        "我遇到了一个说不清的情况", "这件事有些复杂", "暂时不知道属于什么问题", "我想先说明一个情况",
    ],
}

PREFIXES = ["", "请问，", "想咨询一下，", "麻烦帮我看看，", "最近遇到一个情况，"]
SUFFIXES = ["", "。", "，该怎么处理？", "，能帮忙确认吗？", "，已经有一会儿了。", "，谢谢。"]


RAG_QUESTIONS: Dict[str, List[str]] = {
    "退款政策": [
        "购买后多少天内可以无理由退款？", "退款申请提交后审核需要多久？", "审核通过后钱多久退回？",
        "退款会退到哪里？", "商品已经发货还能直接退款吗？", "质量问题退货运费谁承担？",
        "超过七天还能申请退款吗？", "退货完成前可以退款吗？", "退款的完整时间流程是什么？",
        "三十天以内出现质量问题能否退款？",
    ],
    "订单查询": [
        "在哪里查询订单状态？", "订单有哪些状态？", "已发货七天没收到怎么办？", "物流多久会更新？",
        "订单状态异常需要提供什么？", "订单从待支付到完成有哪些阶段？", "怎么知道订单是否发货？",
        "运输中的订单表示什么？", "已签收之后是什么状态？", "订单长期显示已发货如何处理？",
    ],
    "账户安全": [
        "密码至少需要多少位？", "忘记密码怎么重置？", "异常登录后系统会怎么处理？",
        "怎样提高账户安全性？", "客服会索要密码吗？", "密码应该包含哪些字符？",
        "可以通过什么方式找回密码？", "账户被锁定后会收到通知吗？", "两步验证在哪里开启？",
        "密码能不能告诉客服协助排查？",
    ],
    "技术故障排查": [
        "应用崩溃应该先做什么？", "登录出现401表示什么？", "页面加载慢怎么排查？",
        "支付失败时需要检查什么？", "500错误是什么类型的问题？", "清除缓存后还是崩溃怎么办？",
        "401错误是否一定是服务器故障？", "网络正常但页面慢还能怎么处理？", "服务器500持续出现怎么办？",
        "银行卡余额不足会导致什么问题？",
    ],
    "会员与积分": [
        "消费一元能获得多少积分？", "多少积分可以抵扣一元？", "银卡会员需要消费多少？",
        "金卡会员门槛是多少？", "银卡会员享受几折？", "金卡会员享受几折？",
        "积分多久过期？", "生日月积分有什么优惠？", "会员分为哪些等级？", "五千元累计消费是什么会员？",
    ],
    "配送说明": [
        "标准配送需要几天？", "订单满多少钱标准配送免运费？", "加急配送多久送到？",
        "加急配送运费是多少？", "同城配送多久到？", "同城配送收费多少？",
        "偏远地区会延迟多久？", "每天配送时间是什么时候？", "节假日配送是否可能延迟？",
        "发货后还能修改收货地址吗？",
    ],
}


# 原始题目中有些问题可以由新增专题文档和旧综合文档共同回答。
# 多标签用于避免“检索到了同样正确的新文档，却被旧单标签误判为失败”。
RAG_RELEVANCE_OVERRIDES: Dict[str, List[str]] = {
    "商品已经发货还能直接退款吗？": ["退款政策", "订单修改与取消"],
    "客服会索要密码吗？": ["账户安全", "人工客服与升级处理"],
    "密码能不能告诉客服协助排查？": ["账户安全", "人工客服与升级处理"],
    "登录出现401表示什么？": ["技术故障排查", "登录与验证码排查"],
    "支付失败时需要检查什么？": ["技术故障排查", "支付异常处理"],
    "401错误是否一定是服务器故障？": ["技术故障排查", "登录与验证码排查"],
    "银行卡余额不足会导致什么问题？": ["技术故障排查", "支付异常处理"],
    "发货后还能修改收货地址吗？": ["配送说明", "订单修改与取消"],
}


# 新增 6 篇文档的检索题。部分问题天然跨领域，因此直接标注多个相关文档。
RAG_ADDITIONAL_CASES: List[Dict[str, Any]] = [
    {"question": "申请企业发票需要准备哪些信息？", "relevant_titles": ["发票与开票说明"]},
    {"question": "个人发票的抬头一般怎么填写？", "relevant_titles": ["发票与开票说明"]},
    {"question": "企业开票为什么需要纳税人识别号？", "relevant_titles": ["发票与开票说明"]},
    {"question": "电子发票开好后在哪里下载？", "relevant_titles": ["发票与开票说明"]},
    {"question": "发票抬头写错了如何申请重开？", "relevant_titles": ["发票与开票说明"]},
    {"question": "税号填错后应该提供哪些信息？", "relevant_titles": ["发票与开票说明"]},
    {"question": "购买完成后还能补开发票吗？", "relevant_titles": ["发票与开票说明", "订单查询"]},
    {"question": "怎么确认电子发票是否已经开具？", "relevant_titles": ["发票与开票说明", "订单查询"]},
    {"question": "开票信息修改完成前客服可以直接承诺吗？", "relevant_titles": ["发票与开票说明", "人工客服与升级处理"]},
    {"question": "申请发票时需要提供订单号吗？", "relevant_titles": ["发票与开票说明", "订单查询"]},

    {"question": "在哪里查看当前订阅套餐？", "relevant_titles": ["订阅与账单说明"]},
    {"question": "如何查询下一次自动续费日期？", "relevant_titles": ["订阅与账单说明"]},
    {"question": "账单金额通常由哪些项目组成？", "relevant_titles": ["订阅与账单说明"]},
    {"question": "取消自动续费会让当前服务立即失效吗？", "relevant_titles": ["订阅与账单说明"]},
    {"question": "账单金额不对需要记录什么信息？", "relevant_titles": ["订阅与账单说明"]},
    {"question": "查看历史消费账单应该去哪里？", "relevant_titles": ["订阅与账单说明"]},
    {"question": "订阅套餐变更需要后台核验吗？", "relevant_titles": ["订阅与账单说明", "人工客服与升级处理"]},
    {"question": "自动续费后发生重复扣款怎么办？", "relevant_titles": ["订阅与账单说明", "支付异常处理"]},
    {"question": "核对订阅扣款时可以提供支付密码吗？", "relevant_titles": ["订阅与账单说明", "支付异常处理", "人工客服与升级处理"]},
    {"question": "退款状态能不能只根据账单页面直接确认？", "relevant_titles": ["订阅与账单说明", "退款政策"]},

    {"question": "付款失败时首先应该检查哪些项目？", "relevant_titles": ["支付异常处理", "技术故障排查"]},
    {"question": "显示支付失败但银行卡已扣款怎么办？", "relevant_titles": ["支付异常处理"]},
    {"question": "支付结果不确定时为什么不要连续付款？", "relevant_titles": ["支付异常处理"]},
    {"question": "同一订单扣了两笔钱需要记录什么？", "relevant_titles": ["支付异常处理", "订阅与账单说明"]},
    {"question": "钱扣了但订单还是待支付怎么处理？", "relevant_titles": ["支付异常处理", "订单查询"]},
    {"question": "支付异常持续存在应该联系谁核对流水？", "relevant_titles": ["支付异常处理", "人工客服与升级处理"]},
    {"question": "支付限额会不会导致付款失败？", "relevant_titles": ["支付异常处理"]},
    {"question": "排查支付问题时能向客服发送验证码吗？", "relevant_titles": ["支付异常处理", "人工客服与升级处理"]},
    {"question": "客服能否仅凭截图认定发生了重复扣款？", "relevant_titles": ["支付异常处理"]},
    {"question": "扣款成功但订单状态没有更新该查哪些文档？", "relevant_titles": ["支付异常处理", "订单查询"]},

    {"question": "账号无法登录时可以先检查什么？", "relevant_titles": ["登录与验证码排查", "技术故障排查"]},
    {"question": "登录返回401通常表示什么？", "relevant_titles": ["登录与验证码排查", "技术故障排查"]},
    {"question": "收不到短信验证码需要检查哪些地方？", "relevant_titles": ["登录与验证码排查"]},
    {"question": "收不到邮箱验证码应该查看垃圾邮件吗？", "relevant_titles": ["登录与验证码排查"]},
    {"question": "为什么不能频繁点击发送验证码？", "relevant_titles": ["登录与验证码排查"]},
    {"question": "收到多个验证码时应该使用哪一条？", "relevant_titles": ["登录与验证码排查"]},
    {"question": "登录一直失败需要记录哪些排查信息？", "relevant_titles": ["登录与验证码排查", "技术故障排查"]},
    {"question": "账号异常登录应该转人工吗？", "relevant_titles": ["登录与验证码排查", "账户安全", "人工客服与升级处理"]},
    {"question": "忘记密码和登录失败应该参考哪些说明？", "relevant_titles": ["登录与验证码排查", "账户安全"]},
    {"question": "客服是否会要求我提供登录验证码？", "relevant_titles": ["登录与验证码排查", "账户安全", "人工客服与升级处理"]},

    {"question": "订单是否能取消取决于什么？", "relevant_titles": ["订单修改与取消", "订单查询"]},
    {"question": "待支付订单通常可以直接取消吗？", "relevant_titles": ["订单修改与取消", "订单查询"]},
    {"question": "商品已经发货还能直接修改订单吗？", "relevant_titles": ["订单修改与取消", "配送说明"]},
    {"question": "修改收货地址需要提供什么信息？", "relevant_titles": ["订单修改与取消", "配送说明"]},
    {"question": "发货后需要改地址应该联系谁？", "relevant_titles": ["订单修改与取消", "配送说明"]},
    {"question": "取消已支付订单是否等于退款到账？", "relevant_titles": ["订单修改与取消", "退款政策"]},
    {"question": "已支付订单取消后还要走退款流程吗？", "relevant_titles": ["订单修改与取消", "退款政策"]},
    {"question": "没有订单后台时客服能说地址已修改吗？", "relevant_titles": ["订单修改与取消", "人工客服与升级处理"]},
    {"question": "备货中的订单是否一定可以修改？", "relevant_titles": ["订单修改与取消"]},
    {"question": "订单状态和修改地址应该同时查哪些说明？", "relevant_titles": ["订单修改与取消", "订单查询", "配送说明"]},

    {"question": "用户明确要求真人客服时应该怎么处理？", "relevant_titles": ["人工客服与升级处理"]},
    {"question": "升级人工时需要记录哪些已知信息？", "relevant_titles": ["人工客服与升级处理"]},
    {"question": "什么情况需要转人工核验？", "relevant_titles": ["人工客服与升级处理"]},
    {"question": "涉及真实资金操作时为什么要升级人工？", "relevant_titles": ["人工客服与升级处理", "支付异常处理", "订阅与账单说明"]},
    {"question": "需要后台日志和管理员权限时该怎么办？", "relevant_titles": ["人工客服与升级处理", "技术故障排查"]},
    {"question": "人工交接摘要里可以记录错误码吗？", "relevant_titles": ["人工客服与升级处理", "技术故障排查"]},
    {"question": "转人工过程中哪些敏感信息不能收集？", "relevant_titles": ["人工客服与升级处理", "账户安全"]},
    {"question": "智能客服可以假装已经创建工单吗？", "relevant_titles": ["人工客服与升级处理"]},
    {"question": "大范围紧急故障的交接摘要要记录什么？", "relevant_titles": ["人工客服与升级处理", "技术故障排查"]},
    {"question": "升级技术故障时为什么要保留request_id？", "relevant_titles": ["人工客服与升级处理", "技术故障排查"]},
]


RAG_ANSWERS: List[Dict[str, Any]] = [
    {"question": "购买后多少天内可以无理由退款？", "expected_title": "退款政策", "expected_intent": "refund", "reference_answer": "购买后7天内可以申请无理由退款。", "required_facts": ["7天"]},
    {"question": "退款申请提交后审核需要多久？", "expected_title": "退款政策", "expected_intent": "refund", "reference_answer": "退款申请提交后1-3个工作日内审核。", "required_facts": ["1-3个工作日"]},
    {"question": "退款审核通过后多久退回？", "expected_title": "退款政策", "expected_intent": "refund", "reference_answer": "审核通过后5-7个工作日退回原支付账户。", "required_facts": ["5-7个工作日", "原支付账户"]},
    {"question": "商品发货后申请退款需要先做什么？", "expected_title": "退款政策", "expected_intent": "refund", "reference_answer": "商品已发货时需要先完成退货流程。", "required_facts": ["退货"]},
    {"question": "质量问题退货的运费由谁承担？", "expected_title": "退款政策", "expected_intent": "refund", "reference_answer": "质量问题的退货运费不应由用户承担。", "required_facts": ["质量问题"]},
    {"question": "订单状态通常有哪些？", "expected_title": "订单查询", "expected_intent": "order_status", "reference_answer": "包括待支付、已支付、已发货、运输中、已签收和已完成。", "required_facts": ["待支付", "已发货", "运输中", "已完成"]},
    {"question": "订单已发货七天还没收到怎么办？", "expected_title": "订单查询", "expected_intent": "logistics", "reference_answer": "可以联系客服申请查件。", "required_facts": ["查件"]},
    {"question": "发货后物流信息通常多久更新？", "expected_title": "订单查询", "expected_intent": "logistics", "reference_answer": "通常在发货后24小时内更新。", "required_facts": ["24小时"]},
    {"question": "订单状态异常联系客服要提供什么？", "expected_title": "订单查询", "expected_intent": "order_status", "reference_answer": "需要提供订单号。", "required_facts": ["订单号"]},
    {"question": "怎么确认商品是否已经发货？", "expected_title": "订单查询", "expected_intent": "order_status", "reference_answer": "可以通过订单号查询订单状态。", "required_facts": ["订单号", "订单状态"]},
    {"question": "账户密码至少需要多少位？", "expected_title": "账户安全", "expected_intent": "account_security", "reference_answer": "密码长度至少8位，并包含字母和数字。", "required_facts": ["8位", "字母", "数字"]},
    {"question": "忘记密码可以通过哪些方式重置？", "expected_title": "账户安全", "expected_intent": "account_security", "reference_answer": "可以通过绑定的手机号或邮箱重置。", "required_facts": ["手机号", "邮箱"]},
    {"question": "发现异常登录后系统会如何处理？", "expected_title": "账户安全", "expected_intent": "account_security", "reference_answer": "系统会锁定账户并发送通知。", "required_facts": ["锁定", "通知"]},
    {"question": "怎样进一步提高账户安全性？", "expected_title": "账户安全", "expected_intent": "account_security", "reference_answer": "可以在安全设置中开启两步验证。", "required_facts": ["两步验证"]},
    {"question": "客服人员会向我索取密码吗？", "expected_title": "账户安全", "expected_intent": "account_security", "reference_answer": "不会，不要把密码分享给任何人。", "required_facts": ["不会", "密码"]},
    {"question": "应用崩溃时首先可以怎么处理？", "expected_title": "技术故障排查", "expected_intent": "technical_crash", "reference_answer": "清除缓存后重启应用，并更新到最新版本。", "required_facts": ["清除缓存", "重启"]},
    {"question": "登录返回401通常代表什么？", "expected_title": "技术故障排查", "expected_intent": "technical_login", "reference_answer": "401通常表示认证失败。", "required_facts": ["认证失败"]},
    {"question": "页面加载很慢可以检查什么？", "expected_title": "技术故障排查", "expected_intent": "technical", "reference_answer": "检查网络连接，并尝试切换WiFi或移动数据。", "required_facts": ["网络", "WiFi"]},
    {"question": "支付失败时应该先检查什么？", "expected_title": "技术故障排查", "expected_intent": "payment_issue", "reference_answer": "检查支付账户余额和网上支付功能。", "required_facts": ["余额", "网上支付"]},
    {"question": "接口持续返回500应该怎么办？", "expected_title": "技术故障排查", "expected_intent": "technical_crash", "reference_answer": "500是服务端问题，可以稍后重试，持续出现则联系技术支持。", "required_facts": ["服务端", "稍后重试"]},
    {"question": "消费一元可以获得多少积分？", "expected_title": "会员与积分", "expected_intent": "query", "reference_answer": "每消费1元累计1积分。", "required_facts": ["1积分"]},
    {"question": "多少积分可以抵扣一元？", "expected_title": "会员与积分", "expected_intent": "query", "reference_answer": "100积分可以抵扣1元。", "required_facts": ["100积分", "1元"]},
    {"question": "银卡会员需要累计消费多少？", "expected_title": "会员与积分", "expected_intent": "query", "reference_answer": "累计消费1000元可成为银卡会员。", "required_facts": ["1000元"]},
    {"question": "金卡会员可以享受几折优惠？", "expected_title": "会员与积分", "expected_intent": "query", "reference_answer": "金卡会员享受9折优惠。", "required_facts": ["9折"]},
    {"question": "积分有效期是多久？", "expected_title": "会员与积分", "expected_intent": "query", "reference_answer": "积分有效期为1年。", "required_facts": ["1年"]},
    {"question": "标准配送一般几天送到？", "expected_title": "配送说明", "expected_intent": "logistics", "reference_answer": "标准配送一般3-5个工作日送达。", "required_facts": ["3-5个工作日"]},
    {"question": "标准配送满多少钱免运费？", "expected_title": "配送说明", "expected_intent": "logistics", "reference_answer": "订单满99元免标准配送运费。", "required_facts": ["99元"]},
    {"question": "加急配送需要多久，运费多少？", "expected_title": "配送说明", "expected_intent": "logistics", "reference_answer": "加急配送1-2个工作日，运费15元。", "required_facts": ["1-2个工作日", "15元"]},
    {"question": "偏远地区配送会额外增加多久？", "expected_title": "配送说明", "expected_intent": "logistics", "reference_answer": "偏远地区可能额外增加2-3天。", "required_facts": ["2-3天"]},
    {"question": "每天的配送时间是什么时候？", "expected_title": "配送说明", "expected_intent": "logistics", "reference_answer": "每天配送时间为9:00-18:00。", "required_facts": ["9:00-18:00"]},
]


def _jsonl_write(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _jsonl_read(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def generate_datasets() -> Dict[str, int]:
    rng = random.Random(20260907)
    labels = list(INTENT_BASES)
    intent_rows: List[Dict[str, Any]] = []
    for label_index, label in enumerate(labels):
        target = 27 if label_index < 6 else 26
        candidates = []
        for base in INTENT_BASES[label]:
            for prefix in PREFIXES:
                for suffix in SUFFIXES:
                    text = f"{prefix}{base}{suffix}".strip()
                    if text not in candidates:
                        candidates.append(text)
        rng.shuffle(candidates)
        for index, message in enumerate(candidates[:target]):
            intent_rows.append({
                "id": f"intent-{label_index + 1:02d}-{index + 1:03d}",
                "message": message,
                "expected_intent": label,
                "source": "template_synthetic",
                "review_status": "not_human_reviewed",
                "dataset_version": "intent-synthetic-v1",
            })
    rng.shuffle(intent_rows)
    _jsonl_write(INTENT_PATH, intent_rows)

    retrieval_rows = []
    for title, questions in RAG_QUESTIONS.items():
        for question in questions:
            retrieval_rows.append({
                "id": f"rag-retrieval-{len(retrieval_rows) + 1:03d}",
                "question": question,
                "relevant_titles": RAG_RELEVANCE_OVERRIDES.get(question, [title]),
                "source": "manual_from_default_knowledge",
                "dataset_version": "rag-retrieval-v2",
            })
    for case in RAG_ADDITIONAL_CASES:
        retrieval_rows.append({
            "id": f"rag-retrieval-{len(retrieval_rows) + 1:03d}",
            **case,
            "source": "manual_from_default_knowledge",
            "dataset_version": "rag-retrieval-v2",
        })
    _jsonl_write(RAG_RETRIEVAL_PATH, retrieval_rows)

    answer_rows = []
    for index, row in enumerate(RAG_ANSWERS):
        answer_rows.append({
            "id": f"rag-answer-{index + 1:03d}",
            **row,
            "source": "manual_from_default_knowledge",
            "dataset_version": "rag-answer-v1",
        })
    _jsonl_write(RAG_ANSWER_PATH, answer_rows)
    return {"intent": len(intent_rows), "rag_retrieval": len(retrieval_rows), "rag_answer": len(answer_rows)}


def _classification_metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    labels = sorted({row["expected_intent"] for row in rows} | {row.get("predicted_intent", "error") for row in rows})
    per_class: Dict[str, Dict[str, float]] = {}
    confusion: Dict[str, Dict[str, int]] = {}
    for expected in labels:
        confusion[expected] = dict(Counter(row.get("predicted_intent", "error") for row in rows if row["expected_intent"] == expected))
    for label in labels:
        tp = sum(row["expected_intent"] == label and row.get("predicted_intent") == label for row in rows)
        fp = sum(row["expected_intent"] != label and row.get("predicted_intent") == label for row in rows)
        fn = sum(row["expected_intent"] == label and row.get("predicted_intent") != label for row in rows)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        support = sum(row["expected_intent"] == label for row in rows)
        per_class[label] = {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4), "support": support}
    correct = sum(row.get("predicted_intent") == row["expected_intent"] for row in rows)
    macro_f1 = statistics.mean(item["f1"] for item in per_class.values()) if per_class else 0.0
    return {
        "total": len(rows), "correct": correct, "accuracy": round(correct / len(rows), 4) if rows else 0.0,
        "macro_f1": round(macro_f1, 4), "per_class": per_class, "confusion_matrix": confusion,
    }


def _normalize(text: str) -> str:
    # Ignore Markdown and punctuation variants (for example '-'/'–'/'—') when
    # checking deterministic facts, while preserving letters, digits and CJK.
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text, flags=re.UNICODE).lower()


async def _post_with_retries(client: httpx.AsyncClient, url: str, payload: Dict[str, Any], retries: int = 4) -> Dict[str, Any]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
        except Exception as error:
            last = error
            if attempt + 1 < retries:
                await asyncio.sleep(min(8.0, 0.75 * (2 ** attempt)))
    raise RuntimeError(f"request failed after {retries} attempts: {last}")


async def run_intent_benchmark(pi_url: str, concurrency: int, *, use_llm: bool = True,
                               predictions_path: Path | None = None) -> Dict[str, Any]:
    cases = _jsonl_read(INTENT_PATH)
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0
    started = time.perf_counter()

    async with httpx.AsyncClient(timeout=90.0) as client:
        async def one(case: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal completed
            async with semaphore:
                t0 = time.perf_counter()
                try:
                    response = await _post_with_retries(client, f"{pi_url}/intent", {"message": case["message"], "use_llm": use_llm})
                    row = {**case, "predicted_intent": response.get("intent", "other"), "confidence": response.get("confidence"),
                           "source_scores": response.get("source_scores", {}), "reasoning": response.get("reasoning", ""),
                           "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "error": None}
                except Exception as error:
                    row = {**case, "predicted_intent": "error", "confidence": 0, "source_scores": {}, "reasoning": "",
                           "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "error": str(error)}
                completed += 1
                if completed % 50 == 0:
                    print(f"intent progress: {completed}/{len(cases)}", flush=True)
                return row

        rows = await asyncio.gather(*(one(case) for case in cases))
    _jsonl_write(predictions_path or PREDICTIONS_PATH, rows)
    metrics = _classification_metrics(rows)
    metrics.update({
        "duration_s": round(time.perf_counter() - started, 2),
        "latency_ms_mean": round(statistics.mean(row["latency_ms"] for row in rows), 2),
        "latency_ms_p95": round(sorted(row["latency_ms"] for row in rows)[math.ceil(len(rows) * 0.95) - 1], 2),
        "errors": sum(bool(row["error"]) for row in rows),
        "source_usage": {
            "pattern_high_confidence": sum((row.get("source_scores") or {}).get("pattern", 0) >= 0.9 for row in rows),
            "embedding_available": sum((row.get("source_scores") or {}).get("embedding", 0) > 0 for row in rows),
            "llm_used": sum((row.get("source_scores") or {}).get("llm", 0) > 0 for row in rows),
        },
        "failures": [row for row in rows if row.get("predicted_intent") != row["expected_intent"]][:100],
    })
    return metrics


async def run_rag_retrieval(python_url: str, concurrency: int) -> Dict[str, Any]:
    cases = _jsonl_read(RAG_RETRIEVAL_PATH)
    semaphore = asyncio.Semaphore(concurrency)
    report: Dict[str, Any] = {
        "total": len(cases),
        "multi_label_cases": sum(len(case["relevant_titles"]) > 1 for case in cases),
        "k": {},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        for top_k in (1, 3, 5):
            async def one(case: Dict[str, Any]) -> Dict[str, Any]:
                async with semaphore:
                    try:
                        response = await _post_with_retries(client, f"{python_url}/knowledge/search", {"query": case["question"], "top_k": top_k})
                        titles = [str(item.get("title", "")) for item in response.get("results", [])]
                        relevant = set(case["relevant_titles"])
                        hits = relevant.intersection(titles)
                        return {
                            **case,
                            "retrieved_titles": titles,
                            "hit": bool(hits),
                            "all_relevant_hit": hits == relevant,
                            "recall": len(hits) / len(relevant),
                            "error": None,
                        }
                    except Exception as error:
                        return {**case, "retrieved_titles": [], "hit": False, "all_relevant_hit": False, "recall": 0.0, "error": str(error)}
            rows = await asyncio.gather(*(one(case) for case in cases))
            report["k"][str(top_k)] = {
                "hit_at_k": round(sum(row["hit"] for row in rows) / len(rows), 4),
                "recall_at_k": round(statistics.mean(row["recall"] for row in rows), 4),
                "all_relevant_at_k": round(sum(row["all_relevant_hit"] for row in rows) / len(rows), 4),
                "errors": sum(bool(row["error"]) for row in rows),
                "failures": [row for row in rows if not row["all_relevant_hit"]],
                "details": rows,
            }
    return report


async def run_rag_answer_benchmark(python_url: str) -> Dict[str, Any]:
    cases = _jsonl_read(RAG_ANSWER_PATH)

    def dialog_case(case: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "question": case["question"], "expected_intent": case["expected_intent"],
            "reference_answer": case["reference_answer"],
        }

    async with httpx.AsyncClient(timeout=1800.0) as client:
        async def evaluate(selected: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
            raw = await _post_with_retries(client, f"{python_url}/eval/run", {
                "intent_cases": [], "dialog_cases": [dialog_case(case) for case in selected], "record_metrics": False,
            }, retries=2)
            return [result for result in raw.get("results", []) if str(result.get("test_id", "")).startswith("dialog_")]

        results = await evaluate(cases)
        # Transient provider responses occasionally contain no JSON. Re-run
        # only those cases and replace their result, up to three retry rounds.
        for retry_round in range(3):
            failed_indexes = [index for index, result in enumerate(results) if result.get("metadata", {}).get("judge_failed")]
            if not failed_indexes:
                break
            print(f"judge retry round {retry_round + 1}: {len(failed_indexes)} cases", flush=True)
            replacements = await evaluate([cases[index] for index in failed_indexes])
            for index, replacement in zip(failed_indexes, replacements):
                results[index] = replacement
    details = []
    for case, result in zip(cases, results):
        metadata = result.get("metadata", {})
        answer = str(metadata.get("response", ""))
        normalized = _normalize(answer)
        missing = [fact for fact in case["required_facts"] if _normalize(fact) not in normalized]
        scores = result.get("scores", {})
        fact_pass = not missing
        judge_pass = float(scores.get("accuracy", 0)) >= 0.8 and float(scores.get("overall", 0)) >= 0.7
        rules_pass = not metadata.get("rule_violations", [])
        details.append({
            "id": case["id"], "question": case["question"], "answer": answer,
            "expected_intent": case["expected_intent"], "predicted_intent": metadata.get("predicted_intent"),
            "knowledge_used": metadata.get("knowledge_used"), "scores": scores, "missing_facts": missing,
            "fact_pass": fact_pass, "judge_pass": judge_pass, "rules_pass": rules_pass,
            "strict_pass": fact_pass and judge_pass and rules_pass,
            "judge_failed": metadata.get("judge_failed", False), "judge_error": metadata.get("judge_error"),
        })
    score_keys = ("relevance", "accuracy", "completeness", "helpfulness", "overall")
    return {
        "total": len(details),
        "fact_pass_rate": round(sum(item["fact_pass"] for item in details) / len(details), 4) if details else 0.0,
        "judge_pass_rate": round(sum(item["judge_pass"] for item in details) / len(details), 4) if details else 0.0,
        "strict_answer_pass_rate": round(sum(item["strict_pass"] for item in details) / len(details), 4) if details else 0.0,
        "knowledge_used_rate": round(sum(item["knowledge_used"] is True for item in details) / len(details), 4) if details else 0.0,
        "intent_accuracy": round(sum(item["predicted_intent"] == item["expected_intent"] for item in details) / len(details), 4) if details else 0.0,
        "average_scores": {key: round(statistics.mean(float(item["scores"].get(key, 0)) for item in details), 4) for key in score_keys} if details else {},
        "judge_failures": sum(bool(item["judge_failed"]) for item in details),
        "failures": [item for item in details if not item["strict_pass"]],
        "details": details,
    }


async def run_all(pi_url: str, python_url: str, concurrency: int, skip_answers: bool) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        for url in (f"{pi_url}/health", f"{python_url}/health"):
            response = await client.get(url)
            response.raise_for_status()
    report: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "methodology": {
            "intent_dataset": "500 template-generated synthetic cases; not human reviewed",
            "intent_chain": "POST /intent with use_llm=true (rule -> embedding candidates -> LLM)",
            "rag_retrieval": f"{len(_jsonl_read(RAG_RETRIEVAL_PATH))} manually derived questions over twelve built-in knowledge documents; supports multiple relevant titles",
            "rag_answer": "30 questions; deterministic required-fact coverage + LLM-as-Judge + business rules",
            "warning": "Do not present these results as production performance or a human Gold benchmark.",
        },
    }
    print("running intent benchmark", flush=True)
    report["intent"] = await run_intent_benchmark(pi_url, concurrency)
    print("running RAG retrieval benchmark", flush=True)
    report["rag_retrieval"] = await run_rag_retrieval(python_url, concurrency)
    if not skip_answers:
        print("running RAG answer benchmark", flush=True)
        report["rag_answer"] = await run_rag_answer_benchmark(python_url)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "intent", "retrieval", "answers", "run", "all"), nargs="?", default="all")
    parser.add_argument("--pi-url", default="http://127.0.0.1:3000")
    parser.add_argument("--python-url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--skip-answers", action="store_true")
    args = parser.parse_args()
    if args.command in {"generate", "all"}:
        counts = generate_datasets()
        print(f"generated: {counts}")
    if args.command in {"run", "all"}:
        report = asyncio.run(run_all(args.pi_url.rstrip("/"), args.python_url.rstrip("/"), max(1, args.concurrency), args.skip_answers))
        print(json.dumps({
            "intent": {k: report["intent"].get(k) for k in ("total", "correct", "accuracy", "macro_f1", "errors", "duration_s")},
            "rag_retrieval": report["rag_retrieval"].get("k"),
            "rag_answer": {k: report.get("rag_answer", {}).get(k) for k in ("total", "fact_pass_rate", "judge_pass_rate", "strict_answer_pass_rate", "knowledge_used_rate", "intent_accuracy", "judge_failures")},
            "report": str(REPORT_PATH),
        }, ensure_ascii=False, indent=2))
    elif args.command in {"intent", "retrieval", "answers"}:
        async def run_part() -> Dict[str, Any]:
            if args.command == "intent":
                return {"intent": await run_intent_benchmark(args.pi_url.rstrip("/"), max(1, args.concurrency))}
            if args.command == "retrieval":
                return {"rag_retrieval": await run_rag_retrieval(args.python_url.rstrip("/"), max(1, args.concurrency))}
            return {"rag_answer": await run_rag_answer_benchmark(args.python_url.rstrip("/"))}

        partial = asyncio.run(run_part())
        existing = json.loads(REPORT_PATH.read_text(encoding="utf-8")) if REPORT_PATH.exists() else {"timestamp": datetime.now().isoformat()}
        existing.update(partial)
        REPORT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**partial, "report": str(REPORT_PATH)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
