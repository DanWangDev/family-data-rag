"""Generate editable Azure proposal diagrams. No document data or network access.

Run with Python 3.14 from any directory. Pillow is optional (--png previews).
The generator overwrites its generated files; edit native files independently.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

OUT = Path(__file__).resolve().parent
INK = "#243247"
MUTED = "#56667A"
BLUE = "#2563A6"
GREEN = "#24785A"
PURPLE = "#7852A6"
ORANGE = "#A56519"


class Scene:
    def __init__(self, name, title, subtitle, width, height):
        self.name, self.width, self.height = name, width, height
        self.items, self.edges = [], []
        self.text("title", title, 55, 30, width - 110, 55, size=36)
        self.text("subtitle", subtitle, 57, 96, width - 114, 38, size=21, color=MUTED)

    def box(self, key, label, x, y, w, h, *, parent=None, fill="#FFFFFF",
            stroke=INK, boundary=False, dash=False, size=22):
        self.items.append(dict(id=key, text=label, x=x, y=y, w=w, h=h,
                               parent=parent, fill=fill, stroke=stroke,
                               boundary=boundary, dash=dash, size=size,
                               color=stroke if boundary else INK, kind="box"))
        return key

    def text(self, key, label, x, y, w, h, *, size=20, color=INK):
        self.items.append(dict(id=key, text=label, x=x, y=y, w=w, h=h,
                               parent=None, size=size, color=color, kind="text"))

    def get(self, key):
        return next(i for i in self.items if i["id"] == key)

    def port(self, key, side, fraction=.5):
        b = self.get(key)
        return {"left": (b["x"], b["y"] + b["h"] * fraction),
                "right": (b["x"] + b["w"], b["y"] + b["h"] * fraction),
                "top": (b["x"] + b["w"] * fraction, b["y"]),
                "bottom": (b["x"] + b["w"] * fraction, b["y"] + b["h"])}[side]

    def edge(self, key, source, target, *, start="right", end="left",
             sf=.5, tf=.5, via=(), dash=False, color=MUTED):
        points = [self.port(source, start, sf), *via, self.port(target, end, tf)]
        self.edges.append(dict(id=key, source=source, target=target,
                               start=start, end=end, sf=sf, tf=tf,
                               points=points, dash=dash, color=color))


def network():
    s = Scene("01-network-boundaries", "01 / 网络与托管服务边界",
              "AZURE 迁移建议 · 未部署    |    VNet 是网络边界；Private Link 不改变明文处理与数据驻留规则", 2260, 1280)
    s.box("home", "本地 / 家庭网络", 55, 215, 260, 790,
          fill="#F6F8FB", stroke=MUTED, boundary=True)
    s.box("azure", "你的 Azure 订阅  /  资源归属、RBAC 与费用边界；不是网络边界", 345, 155, 1860, 890,
          fill="#FAFBFD", stroke=MUTED, boundary=True, dash=True)
    s.box("vnet", "你的 VNet  /  Azure 对应 VPC 的网络边界", 380, 220, 1080, 785,
          parent="azure", fill="#EEF5FE", stroke=BLUE, boundary=True)
    s.box("provider", "微软托管服务域  /  VNet 外", 1515, 220, 650, 785,
          parent="azure", fill="#F4EFFB", stroke=PURPLE, boundary=True)
    s.box("gateway-subnet", "GatewaySubnet", 405, 300, 225, 220,
          parent="vnet", fill="#F9FBFF", stroke=BLUE, boundary=True, size=21)
    s.box("compute", "应用专用子网", 670, 300, 375, 625,
          parent="vnet", fill="#F9FBFF", stroke=BLUE, boundary=True)
    s.box("pe-subnet", "Private Endpoint 子网", 1090, 300, 330, 625,
          parent="vnet", fill="#F9FBFF", stroke=BLUE, boundary=True, size=21)
    s.box("devices", "家庭设备 / NAS\n浏览器问答\n文档上传", 80, 365, 210, 130,
          parent="home", fill="#FFFFFF")
    s.box("vpn", "VPN Gateway\n加密隧道入口", 425, 375, 185, 110,
          parent="gateway-subnet", fill="#DBEAFF", stroke=BLUE)
    s.box("api", "内部 Web / API\n问答 · 上传 · 引用下载\n身份验证 + 文档 ACL", 695, 385, 325, 140,
          parent="compute", fill="#FFFFFF", stroke=BLUE)
    s.box("worker", "导入 Worker / Jobs\n解析 · OCR · 抽取\n切块 · 索引构建", 695, 600, 325, 140,
          parent="compute", fill="#FFFFFF", stroke=BLUE)
    s.box("runtime-note", "Container Apps 内部环境\nVNet 集成；无公网业务入口\n服务使用 Managed Identity", 695, 785, 325, 110,
          parent="compute", fill="#E7F0FE", stroke=BLUE, size=19)
    s.box("dns-note", "私有 DNS 配置\nPrivate DNS Zones\n关联 VNet\nVPN 须解析私有域名\n必要时配置 DNS 转发", 405, 600, 225, 230,
          parent="vnet", fill="#FFFFFF", stroke=BLUE, size=18)
    s.box("local-note", "本地仍可保留\n加密备份 / 导出副本\n\nVPN 不代替应用登录\n密码与恢复密钥\n不上云", 80, 650, 210, 250,
          parent="home", fill="#FFFFFF", stroke=MUTED, size=19)

    services = [
        ("blob", "Blob PE", "Blob Storage\n原文件 · 标准化资料 · 导出", 365),
        ("pg", "PostgreSQL PE", "PostgreSQL + pgvector\n元数据 · 交易 · 检索索引", 475),
        ("ocr", "OCR PE", "Document Intelligence\nOCR · 表格 · 版面解析", 585),
        ("llm", "OpenAI PE", "Azure OpenAI\nEmbedding · 抽取 · 回答", 695),
        ("vault", "Key Vault PE", "Key Vault\n密钥与必需的秘密", 805),
    ]
    for key, pe, label, y in services:
        s.box(f"pe-{key}", pe, 1120, y, 270, 80, parent="pe-subnet",
              fill="#DBEAFF", stroke=BLUE, size=21)
        s.box(key, label, 1545, y, 590, 80, parent="provider",
              fill="#FFFFFF", stroke=PURPLE, size=22)
        s.edge(f"app-{key}", "compute", f"pe-{key}",
               sf=(y + 40 - 300) / 625, color=BLUE)
        s.edge(f"link-{key}", f"pe-{key}", key, color=PURPLE)
    s.edge("home-vpn", "devices", "vpn", sf=.5, tf=.5,
           via=[(355, 430)], color=BLUE)
    s.edge("vpn-api", "vpn", "api", sf=.5, tf=45 / 140, color=BLUE)
    s.edge("enqueue", "api", "worker", start="bottom", end="top", color=BLUE)
    s.text("enqueue-label", "入库任务", 875, 548, 130, 30, size=19, color=BLUE)
    s.text("link-label", "Private Link", 1405, 329, 155, 30, size=18, color=PURPLE)
    s.text("pe-note", "每项服务独立 PE", 1125, 940, 290, 35, size=19, color=BLUE)
    s.text("ms-note", "服务由微软运行；各服务数据面均关闭公网访问", 1545, 940, 600, 35,
           size=20, color=PURPLE)

    s.box("identity", "身份边界\nEntra ID：MFA / 用户令牌 / 服务身份\n订阅 RBAC 与文档 ACL 分别执行", 55, 1090, 680, 125,
          fill="#F6F8FB", stroke=MUTED, size=21)
    s.box("egress", "网络约束\n私网入口 + 关闭服务公网 + 出站白名单\n平台依赖、DNS、登录及管理流量单独配置", 785, 1090, 680, 125,
          fill="#F6F8FB", stroke=MUTED, size=21)
    s.box("residency", "托管与地域约束\nPE 不代表服务商无法处理明文\n核对模型部署类型、OCR 留存与备份地域", 1515, 1090, 690, 125,
          fill="#FFF6E9", stroke=ORANGE, size=21)
    s.text("footer", "PostgreSQL 采用 Private Link 模式，非 VNet 注入。子网连线表示应用网络访问，不代表允许任意主体访问全部数据。", 57, 1236, 2140, 30,
           size=18, color=MUTED)
    return s


def domains():
    s = Scene("02-service-domains", "02 / 业务服务域与可迁移边界",
              "逻辑服务域 ≠ 子网 ≠ 独立微服务    |    初期可部署为一个 API + 一个 Worker；Azure SDK 集中在供应商适配层", 2260, 1540)
    s.box("access", "访问与权限域", 55, 235, 390, 630,
          fill="#EEF5FE", stroke=BLUE, boundary=True)
    s.box("documents", "文档与证据域", 585, 235, 670, 630,
          fill="#EDF7F1", stroke=GREEN, boundary=True)
    s.box("query", "检索与问答域", 1410, 235, 795, 630,
          fill="#EEF5FE", stroke=BLUE, boundary=True)

    s.box("api", "Web / API\n登录验证 · 上传 · 问答\n原文件与引用下载入口", 85, 335, 330, 150,
          parent="access", stroke=BLUE)
    s.box("acl", "授权上下文 / 文档 ACL\n检索前过滤\n下载、缓存、统计同样校验权限", 85, 580, 330, 155,
          parent="access", stroke=BLUE, size=21)
    s.box("ingest", "导入 / 生命周期\n内容哈希 · 去重 · 快照 · 版本", 620, 335, 600, 110,
          parent="documents", stroke=GREEN)
    s.box("parse", "解析 / OCR 标准化\n文本 · 表格 · 页码 · 区域 · 置信度", 620, 515, 600, 110,
          parent="documents", stroke=GREEN)
    s.box("facts", "结构化事实 / 人工复核\n交易 · 金额 · 日期 · 纠正记录", 620, 695, 600, 110,
          parent="documents", stroke=GREEN)

    s.box("index", "索引构建与发布\n切块 · Embedding · 全文索引 · 索引版本", 1445, 335, 725, 110,
          parent="query", stroke=BLUE)
    s.box("retrieve", "查询执行：先权限与范围过滤\n混合召回 · 重排 · 证据去重 · SQL 统计", 1445, 515, 725, 110,
          parent="query", stroke=BLUE)
    s.box("answer", "回答与引用\nToken 预算 · 来源冲突 · 证据不足时拒答", 1445, 695, 725, 110,
          parent="query", stroke=BLUE)

    s.edge("api-acl", "api", "acl", start="bottom", end="top", color=BLUE)
    s.edge("upload", "api", "ingest", color=GREEN)
    s.text("upload-label", "导入任务", 455, 350, 125, 30, size=19, color=GREEN)
    s.edge("ingest-parse", "ingest", "parse", start="bottom", end="top", color=GREEN)
    s.edge("parse-facts", "parse", "facts", start="bottom", end="top", color=GREEN)
    s.edge("publish", "documents", "index", sf=155 / 630, color=GREEN)
    s.text("publish-label", "文档处理完成后\n构建 / 发布索引", 1268, 335, 140, 70, size=17, color=GREEN)
    s.edge("acl-query", "acl", "retrieve", start="bottom", end="top",
           via=[(250, 915), (1365, 915), (1365, 475), (1807.5, 475)], color=BLUE)
    s.text("acl-query-label", "带已验证身份与授权范围的查询", 605, 877, 650, 30, size=20, color=BLUE)
    s.edge("retrieve-answer", "retrieve", "answer", start="bottom", end="top", color=BLUE)

    s.box("canonical", "权威数据  /  必须可导出", 585, 1000, 670, 205,
          fill="#EDF7F1", stroke=GREEN, boundary=True)
    s.box("canonical-body", "原文件 · 标准化 OCR / 表格 · 页码与坐标\n权限 · 版本 · 事实 · 人工纠正\nBlob + PostgreSQL；独立于 AI 供应商", 610, 1065, 620, 110,
          parent="canonical", stroke=GREEN, size=21)
    s.box("derived", "派生数据  /  允许重建", 1410, 1000, 795, 205,
          fill="#EEF5FE", stroke=BLUE, boundary=True)
    s.box("derived-body", "Chunks · 向量 · 全文索引 · 索引 generation\n记录解析器、切块、Embedding 模型与版本\n更换 Embedding 模型通常需要重新计算向量", 1435, 1065, 745, 110,
          parent="derived", stroke=BLUE, size=21)
    s.edge("doc-store", "documents", "canonical", start="bottom", end="top", color=GREEN)
    s.edge("query-store", "query", "derived", start="bottom", end="top", color=BLUE)
    s.edge("rebuild", "canonical", "derived", color=GREEN)
    s.text("rebuild-label", "可重建", 1281, 1060, 115, 30, size=20, color=GREEN)

    s.box("operations", "安全与运维域", 55, 1000, 390, 455,
          fill="#F6F8FB", stroke=MUTED, boundary=True)
    s.box("ops-body", "最小权限 / 托管身份\n无正文日志 / 审计\n删除与留存策略\n备份 / 导出 / 恢复演练\n评测集 / 成本与质量监测\n\n横跨所有服务域", 85, 1080, 330, 330,
          parent="operations", stroke=MUTED, size=22)

    s.box("adapters", "供应商适配域  /  项目自己的接口；唯一集中依赖 Azure SDK 的层", 585, 1290, 1620, 165,
          fill="#F4EFFB", stroke=PURPLE, boundary=True)
    adapter_labels = [("ocr-adapter", "OCR Adapter\nDocument Intelligence"),
                      ("embed-adapter", "Embedding Adapter\nAzure OpenAI"),
                      ("llm-adapter", "LLM Adapter\nAzure OpenAI"),
                      ("store-adapter", "Storage / Search Adapter\nBlob / PostgreSQL")]
    for i, (key, label) in enumerate(adapter_labels):
        s.box(key, label, 610 + i * 395, 1355, 370, 75,
              parent="adapters", stroke=PURPLE, size=20)
    s.edge("docs-adapter", "documents", "adapters", start="left", sf=.9,
           end="left", tf=.65, via=[(520, 802), (520, 1397.25)], dash=True, color=PURPLE)
    s.edge("query-adapter", "query", "adapters", start="right", sf=.9,
           end="right", tf=.65, via=[(2235, 802), (2235, 1397.25)], dash=True, color=PURPLE)
    s.text("adapter-note", "虚线：供应商调用依赖。业务域通过适配层访问托管服务；数据读写由各业务域按所有权执行。", 590, 1230, 1610, 35,
           size=20, color=PURPLE)
    s.text("footer", "业务域表示职责与所有权，不自动产生网络或进程隔离。金额汇总由 SQL / 代码完成；模型负责抽取辅助与有证据的解释。", 57, 1485, 2140, 32,
           size=20, color=MUTED)
    return s


def seed(key):
    return int(hashlib.sha256(key.encode()).hexdigest()[:7], 16)


def base_element(key, kind, x, y, w, h, stroke=INK, fill="transparent"):
    return dict(id=key, type=kind, x=x, y=y, width=w, height=h, angle=0,
                strokeColor=stroke, backgroundColor=fill, fillStyle="solid",
                strokeWidth=2, strokeStyle="solid", roughness=0, opacity=100,
                groupIds=[], frameId=None, roundness={"type": 3} if kind == "rectangle" else None,
                seed=seed(key), version=1, versionNonce=seed(key + "-v"),
                isDeleted=False, boundElements=[], updated=0, created=None,
                link=None, locked=False)


def text_rect(item):
    lines = item["text"].splitlines()
    height = len(lines) * item["size"] * 1.4
    if item["kind"] == "text":
        return item["x"], item["y"], item["w"], height, "left"
    if item["boundary"]:
        return item["x"] + 20, item["y"] + 17, item["w"] - 40, height, "left"
    return item["x"] + 14, item["y"] + (item["h"] - height) / 2, item["w"] - 28, height, "center"


def excalidraw(scene):
    shapes, texts, arrows = [], [], []
    by_id = {}
    for i in scene.items:
        if i["kind"] == "box":
            b = base_element(i["id"], "rectangle", i["x"], i["y"], i["w"], i["h"], i["stroke"], i["fill"])
            b["strokeStyle"] = "dashed" if i["dash"] else "solid"
            if i["boundary"]:
                b["groupIds"] = [i["id"] + "-heading"]
            else:
                b["boundElements"].append({"id": i["id"] + "-text", "type": "text"})
            by_id[i["id"]] = b
            shapes.append(b)
        x, y, w, h, align = text_rect(i)
        t = base_element(i["id"] + "-text", "text", x, y, w, h, i["color"])
        t.update(text=i["text"], originalText=i["text"], fontSize=i["size"],
                 fontFamily=2, textAlign=align, verticalAlign="middle",
                 containerId=i["id"] if i["kind"] == "box" and not i["boundary"] else None,
                 autoResize=False, lineHeight=1.4)
        if i["kind"] == "box" and i["boundary"]:
            t["groupIds"] = [i["id"] + "-heading"]
        texts.append(t)
    for e in scene.edges:
        x, y = e["points"][0]
        pts = [[px - x, py - y] for px, py in e["points"]]
        a = base_element(e["id"], "arrow", x, y,
                         max(p[0] for p in pts) - min(p[0] for p in pts),
                         max(p[1] for p in pts) - min(p[1] for p in pts), e["color"])
        a.update(points=pts, lastCommittedPoint=None, startArrowhead=None,
                 endArrowhead="arrow", elbowed=False,
                 strokeStyle="dashed" if e["dash"] else "solid")
        for which, key, side, f in [("startBinding", e["source"], e["start"], e["sf"]),
                                    ("endBinding", e["target"], e["end"], e["tf"])]:
            fixed = {"left": [0, f], "right": [1, f], "top": [f, 0], "bottom": [f, 1]}[side]
            a[which] = dict(elementId=key, focus=2 * f - 1, gap=0, fixedPoint=fixed)
            by_id[key]["boundElements"].append({"id": e["id"], "type": "arrow"})
        arrows.append(a)
    # Keep connector strokes behind labels and above the background shapes.
    return dict(type="excalidraw", version=2, source="https://excalidraw.com",
                elements=shapes + arrows + texts,
                appState=dict(viewBackgroundColor="#FFFFFF", gridSize=None,
                              currentItemFontFamily=2), files={})


def drawio(scenes):
    root = ET.Element("mxfile", host="app.diagrams.net", agent="family-data-rag", version="24.7.17")
    for s in scenes:
        page = ET.SubElement(root, "diagram", id=s.name, name=s.name)
        model = ET.SubElement(page, "mxGraphModel", dx="0", dy="0", grid="1", gridSize="10",
                              guides="1", tooltips="1", connect="1", arrows="1", fold="1",
                              page="1", pageScale="1", pageWidth=str(s.width),
                              pageHeight=str(s.height), math="0", shadow="0")
        cells = ET.SubElement(model, "root")
        ET.SubElement(cells, "mxCell", id="0")
        ET.SubElement(cells, "mxCell", id="1", parent="0")
        for i in s.items:
            is_box = i["kind"] == "box"
            style = ("rounded=1;arcSize=8;whiteSpace=wrap;html=1;" if is_box else "text;html=1;whiteSpace=wrap;")
            style += f"fontFamily=Microsoft YaHei;fontSize={i['size']};fontColor={i['color']};"
            if is_box:
                style += f"fillColor={i['fill']};strokeColor={i['stroke']};strokeWidth=2;"
                style += "dashed=1;" if i["dash"] else ""
                style += "align=left;verticalAlign=top;spacingLeft=20;spacingTop=17;container=1;collapsible=0;" if i["boundary"] else "align=center;verticalAlign=middle;spacing=10;"
            else:
                style += "align=left;verticalAlign=top;fillColor=none;strokeColor=none;"
            label = "<br>".join(html.escape(line) or "&nbsp;" for line in i["text"].splitlines())
            c = ET.SubElement(cells, "mxCell", id=i["id"], value=label, style=style,
                              vertex="1", parent=i["parent"] or "1")
            p = s.get(i["parent"]) if i["parent"] else {"x": 0, "y": 0}
            ET.SubElement(c, "mxGeometry", x=str(i["x"] - p["x"]), y=str(i["y"] - p["y"]),
                          width=str(i["w"]), height=str(i["h"]), **{"as": "geometry"})
        for e in s.edges:
            def port(side, f):
                return {"left": (0, f), "right": (1, f), "top": (f, 0), "bottom": (f, 1)}[side]
            sx, sy = port(e["start"], e["sf"])
            tx, ty = port(e["end"], e["tf"])
            style = (f"edgeStyle=none;rounded=0;html=1;strokeColor={e['color']};strokeWidth=2;"
                     f"endArrow=open;endFill=0;exitX={sx};exitY={sy};exitPerimeter=0;"
                     f"entryX={tx};entryY={ty};entryPerimeter=0;")
            if e["dash"]:
                style += "dashed=1;"
            c = ET.SubElement(cells, "mxCell", id=e["id"], style=style, edge="1", parent="1",
                              source=e["source"], target=e["target"])
            geo = ET.SubElement(c, "mxGeometry", relative="1", **{"as": "geometry"})
            if len(e["points"]) > 2:
                arr = ET.SubElement(geo, "Array", **{"as": "points"})
                for x, y in e["points"][1:-1]:
                    ET.SubElement(arr, "mxPoint", x=str(x), y=str(y))
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def svg(scene):
    ns = "http://www.w3.org/2000/svg"
    ET.register_namespace("", ns)
    tag = lambda n: f"{{{ns}}}{n}"
    root = ET.Element(tag("svg"), width=str(scene.width), height=str(scene.height),
                      viewBox=f"0 0 {scene.width} {scene.height}")
    ET.SubElement(root, tag("title")).text = scene.items[0]["text"]
    ET.SubElement(root, tag("rect"), width="100%", height="100%", fill="#FFFFFF")
    for i in scene.items:
        if i["kind"] == "box":
            a = dict(x=str(i["x"]), y=str(i["y"]), width=str(i["w"]), height=str(i["h"]),
                     rx="12", fill=i["fill"], stroke=i["stroke"], **{"stroke-width": "2"})
            if i["dash"]:
                a["stroke-dasharray"] = "10 7"
            ET.SubElement(root, tag("rect"), a)
    for e in scene.edges:
        attrs = dict(points=" ".join(f"{x},{y}" for x, y in e["points"]), fill="none",
                     stroke=e["color"], **{"stroke-width": "2.2", "stroke-linejoin": "round"})
        if e["dash"]:
            attrs["stroke-dasharray"] = "9 6"
        ET.SubElement(root, tag("polyline"), attrs)
        (px, py), (x, y) = e["points"][-2:]
        angle = math.atan2(y - py, x - px)
        pts = [(x - 12 * math.cos(angle - .45), y - 12 * math.sin(angle - .45)),
               (x, y), (x - 12 * math.cos(angle + .45), y - 12 * math.sin(angle + .45))]
        ET.SubElement(root, tag("polyline"), points=" ".join(f"{x},{y}" for x, y in pts),
                      fill="none", stroke=e["color"], **{"stroke-width": "2.2"})
    for i in scene.items:
        x, y, w, _, align = text_rect(i)
        text = ET.SubElement(root, tag("text"), x=str(x + w / 2 if align == "center" else x),
                             fill=i["color"], **{"font-family": "Microsoft YaHei, Noto Sans CJK SC, sans-serif",
                             "font-size": str(i["size"]), "text-anchor": "middle" if align == "center" else "start"})
        for j, line in enumerate(i["text"].splitlines()):
            ET.SubElement(text, tag("tspan"), x=text.get("x"),
                          y=str(y + i["size"] * (1.07 + j * 1.4))).text = line or " "
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def png(scene, font_path):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (scene.width, scene.height), "white")
    d = ImageDraw.Draw(image)
    for i in scene.items:
        if i["kind"] == "box":
            d.rounded_rectangle((i["x"], i["y"], i["x"] + i["w"], i["y"] + i["h"]),
                                radius=12, fill=i["fill"], outline=i["stroke"], width=2)
    for e in scene.edges:
        if not e["dash"]:
            d.line(e["points"], fill=e["color"], width=2)
        else:
            for (ax, ay), (bx, by) in zip(e["points"], e["points"][1:]):
                length = math.hypot(bx - ax, by - ay)
                for offset in range(0, math.ceil(length), 15):
                    a, b = offset / length, min(offset + 9, length) / length
                    d.line([(ax + (bx - ax) * a, ay + (by - ay) * a),
                            (ax + (bx - ax) * b, ay + (by - ay) * b)], fill=e["color"], width=2)
        (px, py), (x, y) = e["points"][-2:]
        angle = math.atan2(y - py, x - px)
        d.line([(x - 12 * math.cos(angle - .45), y - 12 * math.sin(angle - .45)),
                (x, y), (x - 12 * math.cos(angle + .45), y - 12 * math.sin(angle + .45))],
               fill=e["color"], width=2)
    for i in scene.items:
        x, y, w, _, align = text_rect(i)
        font = ImageFont.truetype(font_path, i["size"])
        for j, line in enumerate(i["text"].splitlines()):
            if d.textlength(line, font=font) > w + 2:
                raise ValueError(f"Text exceeds its box: {i['id']}: {line}")
            d.text((x + w / 2 if align == "center" else x, y + j * i["size"] * 1.4),
                   line, fill=i["color"], font=font, anchor="mt" if align == "center" else "lt")
    image.save(OUT / f"{scene.name}.png")


def validate(scenes):
    for s in scenes:
        data = json.loads((OUT / f"{s.name}.excalidraw").read_text(encoding="utf-8"))
        ids = {i["id"] for i in data["elements"]}
        assert len(ids) == len(data["elements"]), "Duplicate element IDs"
        assert not data["files"], "Native diagrams must not contain raster images"
        for i in data["elements"]:
            assert i["width"] >= 0 and i["height"] >= 0
            for bound in i["boundElements"]:
                assert bound["id"] in ids
            if i["type"] == "arrow":
                for which in ("startBinding", "endBinding"):
                    assert i[which]["elementId"] in ids
            if i["type"] == "text" and i["containerId"]:
                assert i["containerId"] in ids
        for i in s.items:
            assert i["x"] >= 0 and i["y"] >= 0
            assert i["x"] + i["w"] <= s.width and i["y"] + i["h"] <= s.height
            if i["parent"]:
                p = s.get(i["parent"])
                assert p["x"] <= i["x"] and p["y"] <= i["y"]
                assert i["x"] + i["w"] <= p["x"] + p["w"]
                assert i["y"] + i["h"] <= p["y"] + p["h"]
        ET.parse(OUT / f"{s.name}.svg")
        print(f"Validated {s.name}: {len(data['elements'])} editable elements, {len(s.edges)} bound connectors")
    tree = ET.parse(OUT / "azure-architecture.drawio")
    assert len(tree.findall("diagram")) == len(scenes)
    for page in tree.findall("diagram"):
        cells = page.findall("mxGraphModel/root/mxCell")
        ids = {c.get("id") for c in cells}
        assert len(ids) == len(cells)
        for c in cells:
            for field in ("parent", "source", "target"):
                if c.get(field):
                    assert c.get(field) in ids
    print("Validated draw.io XML: 2 pages; all parent/source/target references resolve")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", action="store_true", help="Render PNG previews (requires Pillow)")
    parser.add_argument("--font", default="C:/Windows/Fonts/msyh.ttc", help="CJK font for PNG rendering")
    args = parser.parse_args()
    scenes = [network(), domains()]
    for s in scenes:
        (OUT / f"{s.name}.excalidraw").write_text(json.dumps(excalidraw(s), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (OUT / f"{s.name}.svg").write_bytes(svg(s))
        if args.png:
            png(s, args.font)
    (OUT / "azure-architecture.drawio").write_bytes(drawio(scenes))
    validate(scenes)


if __name__ == "__main__":
    main()
