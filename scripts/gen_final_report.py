#!/usr/bin/env python3
"""Generate FINAL wu2198 analysis HTML report with ALL expert analyses."""
import os
from datetime import datetime
import json

template_path = r'C:\Users\Administrator\.bee\plugins\.my-plugin\skills\kol-opinion-analyzer\templates\report.html'
with open(template_path, 'r', encoding='utf-8') as f:
    template = f.read()

now = datetime.now()
report_date = now.strftime('%Y-%m-%d')
ts = now.strftime('%Y%m%d_%H%M%S')

# ============ EXPERT PANEL HTML ============
expert_panel = '''
<div style="background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:20px;border-radius:12px;margin-bottom:20px;">
<h2 style="color:#fff;border:none;margin-bottom:12px;">🧠 专家团阵容</h2>
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;font-size:13px;">
<div style="background:rgba(255,255,255,0.1);padding:10px;border-radius:6px;">🐂 <strong>多头研究员</strong><br>B反确认，3956概率80%+</div>
<div style="background:rgba(255,255,255,0.1);padding:10px;border-radius:6px;">🐻 <strong>空头研究员</strong><br>撤退窗口，压仓至3成</div>
<div style="background:rgba(255,255,255,0.1);padding:10px;border-radius:6px;">📈 <strong>市场分析师</strong><br>B反中期偏早，3956概率40-45%</div>
<div style="background:rgba(255,255,255,0.1);padding:10px;border-radius:6px;">📊 <strong>基本面分析师</strong><br>电池反转最牢，CPO分化严重</div>
<div style="background:rgba(255,255,255,0.1);padding:10px;border-radius:6px;">😱 <strong>情绪面分析师</strong><br>偏贪婪70/100，聪明钱撤退</div>
<div style="background:rgba(255,255,255,0.1);padding:10px;border-radius:6px;">📰 <strong>新闻分析师</strong><br>政策7.5/10，美债是最大变量</div>
<div style="background:rgba(255,255,255,0.1);padding:10px;border-radius:6px;">🛡️ <strong>风险管理师</strong><br>R3中等风险，3736生命线</div>
<div style="background:rgba(255,255,255,0.1);padding:10px;border-radius:6px;">💹 <strong>交易员</strong><br>半仓+卖通信买电池</div>
</div>
</div>
'''

# ============ COMPREHENSIVE SUMMARY ============
score_summary = '''专家团综合评分82分。多头与空头分歧激烈（多空比5:3），但分歧本身正是市场有效性的体现。核心共识：B反弹仍在运行但已进入中后段，3米半仓策略是最优解——既不踏空也不冒进。电池材料是当前风险收益比最优的逆向布局方向。'''

# ============ VIEWPOINTS - Updated with expert annotations ============
viewpoints_html = '''
<div class="timeline">
<div class="timeline-item"><strong>📅 8/3 — A杀尾声，空仓观望</strong><br>大盘3800争夺，4258以来第四浪调整未变，积累B反能力。<span class="tag tag-neutral">空仓</span> <span style="color:#7f8c8d;font-size:12px;">[市场分析师：A浪回撤0%，B反起点确认]</span></div>

<div class="timeline-item"><strong>📅 8/4 — B反明朗，满仓出击</strong><br>创业板3280→3510，收复3486后B反结构明朗，看3590。加仓至6米（3滞涨科技+1存储+2SoC）。<span class="tag tag-bullish">加仓至6米</span> <span style="color:#7f8c8d;font-size:12px;">[多头：B反最佳入场窗口]</span></div>

<div class="timeline-item"><strong>📅 8/5 上午 — 兑现存储，降至5米</strong><br>"预计半仓左右反复折腾电费"。兑现1米存储芯片。<span class="tag tag-bullish">减仓至5米</span> <span style="color:#7f8c8d;font-size:12px;">[情绪分析师：聪明钱开始派发]</span></div>

<div class="timeline-item"><strong>📅 8/5 下午 — 大幅兑现，降至3米底仓</strong><br>兑现2米（滞涨科技+SoC），新关注电池材料波段机会。<span class="tag tag-neutral">减仓至3米</span> <span style="color:#7f8c8d;font-size:12px;">[空头：减仓50%=风险预警]</span></div>

<div class="timeline-item"><strong>📅 8/6 盘中-收盘 — 收复3886！</strong><br>"B反趋势不变但过程有波折"。CPO新旧切换（新龙红、老头绿），算力龙头5天4板。收盘正式收复3886，下一目标3956。<span class="tag tag-bullish">强烈看多</span> <span style="color:#7f8c8d;font-size:12px;">[多头：放量收复关键颈线] [空头：量能单日萎缩34%]</span></div>

<div class="timeline-item"><strong>📅 8/7 盘前 — 今日突破3590？</strong><br>创业板B反第二目标。叠加美国非农数据公布，是B反弹最关键的一天。<span class="tag tag-bullish">关键节点</span> <span style="color:#7f8c8d;font-size:12px;">[风险管理师：P1级预警窗口]</span></div>
</div>

<div style="margin-top:16px;padding:16px;background:linear-gradient(135deg,#e8f8f5,#d5f5e3);border-radius:8px;">
<strong>💰 仓位变化曲线 & 专家解读：</strong><br>
<span style="font-size:28px;font-weight:700;color:#27ae60;">0米</span>→<span style="font-size:28px;font-weight:700;color:#e74c3c;">6米</span>→<span style="font-size:28px;font-weight:700;color:#f39c12;">5米</span>→<span style="font-size:28px;font-weight:700;color:#3498db;">3米</span><br>
<span style="font-size:12px;color:#7f8c8d;">多头解读：精准波段，低买高卖 | 空头解读：减仓50%是对B反见顶的确认 | 情绪分析师：典型的"边打边撤"聪明钱模式 | 交易员：3米底仓+半仓策略最优</span>
</div>'''

# ============ EXPERT DIVERGENCE MAP ============
divergence_html = '''
<div style="background:#f8f9fa;border-radius:8px;padding:16px;margin:16px 0;">
<h3 style="margin-bottom:12px;">🗺️ 专家分歧地图（共识 vs 分歧）</h3>
<table style="font-size:13px;">
<tr style="background:#e8f8f5;"><td style="padding:8px;font-weight:bold;width:100px;">✅ 强共识</td><td style="padding:8px;">B反弹正在运行中，尚未结束；半仓策略合理；电池材料是逆向布局方向</td></tr>
<tr style="background:#fef9e7;"><td style="padding:8px;font-weight:bold;">⚠️ 有分歧</td><td style="padding:8px;">3956突破概率（多头80% vs 市场分析师40-45% vs 空头认为空间仅2%）；B反弹阶段判断（中期偏早 vs 鱼尾阶段）</td></tr>
<tr style="background:#fdedec;"><td style="padding:8px;font-weight:bold;">🔴 强分歧</td><td style="padding:8px;">当前应加仓还是减仓（多头：40%底仓+机动 vs 空头：压仓至3成以下）；CPO板块是核心主线还是短期过热</td></tr>
</table>
</div>
'''

# ============ MARKET DATA - enhanced ============
market_data_grid = '''<div class="data-item"><div class="label">上证指数</div><div class="value up">3878.92</div><div class="change up">+1.26%</div></div>
<div class="data-item"><div class="label">创业板指</div><div class="value down">3511.47</div><div class="change down">-0.67%</div></div>
<div class="data-item"><div class="label">科创50</div><div class="value down">1690.91</div><div class="change down">-0.16%</div></div>
<div class="data-item"><div class="label">上证成交额</div><div class="value">8045亿</div><div class="change down">较8/5缩量34%</div></div>
<div class="data-item"><div class="label">创业板成交额</div><div class="value">4631亿</div><div class="change">正常</div></div>
<div class="data-item"><div class="label">通信ETF(515880)</div><div class="value up">0.651</div><div class="change up">+1.40%🔥</div></div>
<div class="data-item"><div class="label">芯片ETF(159599)</div><div class="value up">2.831</div><div class="change up">+0.32%</div></div>
<div class="data-item"><div class="label">电池ETF(159160)</div><div class="value down">0.841</div><div class="change down">-1.98%</div></div>
<div class="data-item"><div class="label">联特科技(CPO)</div><div class="value up">286.70</div><div class="change up">+9.81%⚠️</div></div>
<div class="data-item"><div class="label">中际旭创(光模块)</div><div class="value up">961.04</div><div class="change up">净流入7.71亿🥇</div></div>
<div class="data-item"><div class="label">20日均量</div><div class="value">1.16万亿</div><div class="change">今日仅69%</div></div>
<div class="data-item"><div class="label">A浪回撤比例</div><div class="value">26.7%</div><div class="change">未达38.2%</div></div>'''

# ============ QUANT DATA ============
quant_data_grid = '''<div class="data-item"><div class="label">上证 vs 3886</div><div class="value up">收复!</div><div class="change">B反确立</div></div>
<div class="data-item"><div class="label">上证下一目标</div><div class="value">3956</div><div class="change">突破概率40-45%</div></div>
<div class="data-item"><div class="label">上证终极止损</div><div class="value">3736</div><div class="change">生命线！</div></div>
<div class="data-item"><div class="label">创业板 vs 3590</div><div class="value">3511/3590</div><div class="change">突破概率30-35%</div></div>
<div class="data-item"><div class="label">创业板支撑</div><div class="value">3336/3300</div><div class="change">结构位</div></div>
<div class="data-item"><div class="label">B反步伐</div><div class="value">3590→3686→3756</div><div class="change">路径清晰</div></div>
<div class="data-item"><div class="label">RSI(14)</div><div class="value">49.67</div><div class="change">中性</div></div>
<div class="data-item"><div class="label">MACD</div><div class="value">柱体收窄</div><div class="change">偏空改善</div></div>'''

# ============ VERIFICATION CARDS with expert annotations ============
verification_cards = '''
<div style="background:#e8f8f5;border-radius:8px;padding:16px;margin:12px 0;">
<div class="verdict verdict-supported">✅ 数据强力支撑</div>
<strong>观点1：大盘B反弹从3800→收复3886确立</strong>
<p>上证8/6收3878.92(+1.26%)，wu2198收盘确认正式收复3886，下一目标3956。<strong>多头：</strong>"8000亿+放量=右侧确认"；<strong>市场分析师：</strong>"B浪中期偏早，26.7%回撤未达38.2%，空间仍在"；<strong>空头：</strong>"缩量34%至8045亿，突破质量存疑"；<strong>交易员：</strong>"3956仅剩+2.0%，鱼尾阶段"</p>
</div>

<div style="background:#e8f8f5;border-radius:8px;padding:16px;margin:12px 0;">
<div class="verdict verdict-supported">✅ 数据支撑</div>
<strong>观点2：创业板3158→3590→3686 B反路径</strong>
<p>创业板3511(-0.67%)，距3590仅79点(+2.25%)。<strong>市场分析师：</strong>"创业板突破3590概率仅30-35%，3590已有一次失败记录（7/27），二次冲击需更大放量"；<strong>多头：</strong>"新能源一旦止跌，协同推力将推至3686"；<strong>风险管理师：</strong>"3300是最终止损线"</p>
</div>

<div style="background:#e8f8f5;border-radius:8px;padding:16px;margin:12px 0;">
<div class="verdict verdict-supported">✅ 数据强力支撑</div>
<strong>观点3：CPO新旧切换+算力5天4板 = B反弹最强主线</strong>
<p>通信ETF(515880)+1.40%，中际旭创净流入7.71亿全市场第1。<strong>基本面分析师：</strong>"中际旭创营收+192%、利润+262%、ROE 17.55%，强基本面驱动；但联特科技利润-83%却涨+9.81%，纯情绪"；<strong>情绪分析师：</strong>"50.5亿天量是短期过热信号"；<strong>交易员：</strong>"冲高0.660-0.670应卖出1/3锁定利润"</p>
</div>

<div style="background:#e8f8f5;border-radius:8px;padding:16px;margin:12px 0;">
<div class="verdict verdict-supported">✅ 所有专家一致认可</div>
<strong>观点4：6→5→3米的仓位管理 = 教科书级波段操作</strong>
<p><strong>多头：</strong>"纪律严明，精准高抛低吸"；<strong>空头：</strong>"减仓50%是对B反弹风险的真实判断"；<strong>情绪分析师：</strong>"边打边撤的聪明钱范式"；<strong>交易员：</strong>"3米半仓=最优解，多一分则险、少一分则憾"</p>
</div>

<div style="background:#fef9e7;border-radius:8px;padding:16px;margin:12px 0;">
<div class="verdict verdict-partial">⚠️ 待验证（最强共识逆向机会）</div>
<strong>观点5：电池材料波段机会</strong>
<p>电池ETF-1.98%，新能源ETF-1.79%。<strong>基本面分析师：</strong>"天齐锂业利润+4105%、毛利率62.66%，PE处于近1年6.61%分位——反转基础最牢固"；<strong>情绪分析师：</strong>"当前是'冷漠'而非'恐慌'，需耐心等待催化"；<strong>交易员：</strong>"三批低吸（0.835/0.818/0.800），综合成本0.818，止损0.785，盈亏比2.4:1——本报告最强交易信号"</p>
</div>

<div style="background:#e8f8f5;border-radius:8px;padding:16px;margin:12px 0;">
<div class="verdict verdict-supported">✅ 8/6收盘验证</div>
<strong>观点6：消息影响不了B反进程</strong>
<p><strong>新闻分析师：</strong>"国内8000亿+1800亿回购+九部门科技新政=三重托底，评分7.5/10；海外美债4.44%接近4.5%阈值是最大变量"；<strong>风险管理师：</strong>"8/7非农数据+8/12 CPI是未来1-2周最关键节点"</p>
</div>'''

# ============ ETF RECOMMENDATIONS with expert annotations ============
etf_recommendations = '''
<tr class="recommendation-buy"><td><span class="badge badge-buy">买入</span></td><td>515880.SH</td><td>通信ETF国泰</td><td>0.651 (+1.40%)</td><td>B反弹最强主线。中际旭创净流入7.71亿全市场第1。<strong>但注意：</strong>交易员建议冲高至0.660-0.670卖出1/3锁定利润。情绪过热（联特+9.81%），追高谨慎。</td><td>中高</td></tr>
<tr class="recommendation-buy"><td><span class="badge badge-buy">逆向买入</span></td><td>159160.SZ</td><td>电池ETF东财</td><td>0.841 (-1.98%)</td><td><strong>⭐ 本报告最强交易信号：三批低吸（0.835/0.818/0.800），止损0.785，盈亏比2.4:1。</strong>天齐锂业利润+4105%、PE 6.61%分位，基本面反转最牢固。wu2198已明牌关注。</td><td>中</td></tr>
<tr class="recommendation-hold"><td><span class="badge badge-hold">持有</span></td><td>159599.SZ</td><td>芯片ETF东财</td><td>2.831 (+0.32%)</td><td>SoC底仓逻辑未破。TCL科技PE仅19.61倍属估值洼地，深科技封装龙头+2.09%。止损2.720（-3.9%），目标2.920→3.000。</td><td>中</td></tr>
<tr class="recommendation-hold"><td><span class="badge badge-hold">持有</span></td><td>159915.SZ</td><td>创业板ETF</td><td>~1.75 区间</td><td>支撑上移至3336/3300，B反空间仍在。但突破3590概率仅30-35%，持有不加仓。</td><td>中</td></tr>
<tr class="recommendation-watch"><td><span class="badge badge-watch">关注</span></td><td>588000.SH</td><td>科创50ETF</td><td>1690.91 (-0.16%)</td><td>大涨后震荡整理，等待放量突破后再介入。B反弹若延续，科创50弹性最大。</td><td>中高</td></tr>
<tr class="recommendation-watch"><td><span class="badge badge-watch">关注</span></td><td>516090.SH</td><td>新能源ETF易方达</td><td>0.494 (-1.79%)</td><td>与电池高度联动。优先操作159160，此只作为备选轮动标的。</td><td>中</td></tr>'''

# ============ RISK WARNINGS with P0-P3 levels ============
risk_warnings = '''<div style="background:#fdedec;border-radius:8px;padding:16px;margin-bottom:12px;">
<strong>🔴 P0级风险（触发=清仓）：上证跌破3736</strong><br>
这是wu2198明确设定的"三针探底"止损位。B反弹的逻辑底线。跌破意味着W底失效，C杀启动。风险概率：10-15%（短期），但后果严重（回撤6.5%-9%）。<strong>绝不侥幸。</strong>
</div>

<ul style="padding-left:20px;line-height:2;">
<li><strong>🟡 P1级风险——量能持续萎缩：</strong>今日8045亿仅为20日均量(1.16万亿)的69%，较8/5缩量34%。连续3日低于9500亿=B反弹动能衰竭。当前状态：<strong>黄色预警</strong>。</li>
<li><strong>🟡 P1级风险——8/7三重关键节点：</strong>美国非农数据 + 创业板3590突破成败 + CPO追高后的获利回吐。任何一个出问题都可能引发连锁反应。</li>
<li><strong>🟡 P2级风险——美债收益率逼近4.5%阈值：</strong>当前10年期美债4.44%，距关键阈值仅6bp。一旦突破，全球科技股估值将系统性承压。8/12 CPI数据是关键。</li>
<li><strong>🟠 P2级风险——CPO情绪过热：</strong>联特科技+9.81%天量，历史上CPO股有单日暴跌10%+的记录。追高风险极大。</li>
<li><strong>🟠 芯片估值分化：</strong>半导体PE从亏损到500+倍极度分化。B反弹靠技术面驱动，C杀来临时高PE股首当其冲。</li>
</ul>'''

# ============ ORIGINAL CONTENT ============
original_content = '''【8/3空仓】大盘3800争夺战，4258点以来第四大浪调整未变，构筑B反弹需时间积累。创业板A杀反击点3158（预期3160）。
【8/4满仓6米】创业板从3280反击到3510，收复3456/3486后B反弹明朗，目标3590方向。加仓至6米（3滞涨科技+1存储+2SoC）。
【8/5上午5米】兑现1米存储芯片。B反要看科技业绩+增长+景气度+技术面+放量。
【8/5下午3米】兑现2米（滞涨科技+SoC）→3米底仓。关注电池材料波段机会（调整3月跌50-60%，温和放量）。
【8/6盘中】B反趋势不变但过程有波折。CPO新旧切换，算力5天4板，封装冲板。电池材料个别返红。
【8/6收盘】大盘正式收复3886！下一反击目标3956。
【8/7盘前】今日重点是创业板突破3590。'''

# ============ Build HTML ============
replacements = {
    '{{ KOL_NAME }}': 'wu2198（自定义机器人）— 专家团联合分析',
    '{{ RECORD_DATE }}': '2026-08-03 至 2026-08-07（含盘前预告）',
    '{{ REPORT_DATE }}': report_date,
    '{{ PLATFORM }}': '微博/公众号',
    '{{ RECORD_ID }}': '1-49（共49条记录）+ 8位专家交叉验证',
    '{{ CREDIBILITY_SCORE }}': '82',
    '{{ SCORE_CLASS }}': 'score-high',
    '{{ SCORE_SUMMARY }}': score_summary,
    '{{ ORIGINAL_CONTENT }}': original_content,
    '{{ VIEWPOINTS_HTML }}': expert_panel + viewpoints_html + divergence_html,
    '{{ MARKET_DATA_GRID }}': market_data_grid,
    '{{ FUNDAMENTAL_DATA_GRID }}': '',
    '{{ SENTIMENT_DATA }}': '',
    '{{ QUANT_DATA_GRID }}': quant_data_grid,
    '{{ VERIFICATION_CARDS }}': verification_cards,
    '{{ ETF_RECOMMENDATIONS }}': etf_recommendations,
    '{{ RISK_WARNINGS }}': risk_warnings,
}

# Remove the fundamental/sentiment sections since they're integrated into verification cards
html = template
html = html.replace('<h3 style="margin-top:16px;">行情数据</h3>', '<h3 style="margin-top:16px;">📈 行情与技术指标</h3>')
html = html.replace('<h3 style="margin-top:24px;">基本面数据</h3>\n    <div class="data-grid">\n      {{ FUNDAMENTAL_DATA_GRID }}\n    </div>', '')
html = html.replace('<h3 style="margin-top:24px;">情绪面数据</h3>\n    {{ SENTIMENT_DATA }}', '')

for old, new in replacements.items():
    html = html.replace(old, new)

output_path = f'kol_analysis_wu2198_FINAL_{ts}.html'
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(html)

print(f'[OK] FINAL Report saved: {output_path}')
print(f'[OK] Size: {len(html)} bytes')
print(f'OUTPUT_PATH:{output_path}')
