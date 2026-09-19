# -*- coding: utf-8 -*-
import math
def clamp(x,a,b): return max(a,min(b,x))

# ---- 超短线(1-3日) , 满分100 ----
# 动量 25 : RSI(配合位置)+近5日
rsi=38.65
mom_rsi = 60 if 30<=rsi<=45 else (40 if rsi<30 else 35)
# 超跌但未极端 => 中性偏弱
mom5 = -3.33
mom = clamp(mom_rsi + (10 if -5<mom5<-2 else 0), 0, 25)

# 走势/位置 25 : 偏离MA5/MA20
dev_ma5=-1.40; dev_ma20=-3.60
trend = clamp(22 - abs(dev_ma5)*2 - abs(dev_ma20)*1.2, 0, 25)

# 关键位 20 : 距3256
d3256=(3285.58-3256)/3285.58*100  # +0.90%
key = clamp(16 - abs(d3256)*3, 0, 20)

# 量能/资金 15
vol = 6  # 量能16290亿(缩量)6分 ; 指数主力+35.76亿 vs ETF主力-3.48亿 => 中性偏弱
# 波动率/风险 15 : ATR 2.46%, 5日vol 6.81%
vola = clamp(15 - (2.46-1.8)*8, 0, 15)

short_total = mom+trend+key+vol+vola
print("[超短线(1-3日)]")
print(f"  动量(25)   = {mom:.1f}")
print(f"  走势(25)   = {trend:.1f}  (现价低于MA5 {dev_ma5}%, 低于MA20 {dev_ma20}%)")
print(f"  关键位(20) = {key:.1f}  (距3256 +{d3256:.2f}%)")
print(f"  量能资金(15)= {vol:.1f}")
print(f"  波动风险(15)= {vola:.1f}")
print(f"  >>> 超短线综合 = {short_total:.1f} / 100")

# ---- 波段(1-4周) ----
# 中期趋势 30 : MA20/MA60 空头排列, 60日-24.6%
trend_m = clamp(30 - 24.63*0.6 - abs(-9.48)*1.2, 0, 30)  # 回撤惩罚+MA60偏离
# 估值 25 : PE分位24.11%低, PB分位49.57%中
val = 18.5
# 资金 15 : 缩量,两融4周降,ETF折价-1.10%
fund = 7
# 回撤/位置 15 : 距顶-25.0%, 已近A杀低点区
dd = 9
# 情绪/事件 15
sent = 8
wave_total = trend_m+val+fund+dd+sent
print("\n[波段(1-4周)]")
print(f"  中期趋势(30) = {trend_m:.1f}")
print(f"  估值(25)     = {val:.1f}  (PE分位24.1%, PB分位49.6%)")
print(f"  资金(15)     = {fund:.1f}")
print(f"  回撤位置(15) = {dd:.1f}  (距顶-24.85%)")
print(f"  情绪事件(15) = {sent:.1f}")
print(f"  >>> 波段综合 = {wave_total:.1f} / 100")

# 建议仓位
print(f"\n[建议仓位] 超短线 {short_total:.0f}/100 -> 上限约 {clamp((short_total-40)/60*100,0,40):.0f}%")
print(f"[建议仓位] 波段   {wave_total:.0f}/100 -> 参考 {(wave_total-50)/50*100:.0f}%")
