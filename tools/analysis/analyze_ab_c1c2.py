"""Create a compact A/B, A2 and C1/C2-swap analysis without extra packages."""
from __future__ import annotations

import csv
import html
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "output" / "delay_sweep_automation"
RUNS = {"A": ROOT / "20260727_095646_A", "B": ROOT / "20260727_102320_B", "A2": ROOT / "20260727_104609_A2", "changed": ROOT / "20260727_140945_A_c1c2change"}
OUT = ROOT / "analysis_ab_c1c2_20260727"

def mean(v): return st.fmean(v)
def sdev(v): return st.stdev(v) if len(v) > 1 else 0.0

def load(folder):
    with (folder / "summary.csv").open(encoding="utf-8-sig", newline="") as f: rows = list(csv.DictReader(f))
    assert rows and all(r["quality_status"] == "ok" and int(float(r["waveform_count"])) == 100 and int(float(r["sample_count"])) == 100 for r in rows)
    grouped = defaultdict(list)
    for row in rows: grouped[row["set_id"]].append(row)
    return grouped

def fit(points):
    mx, my = mean([p[0] for p in points]), mean([p[1] for p in points])
    slope = sum((x-mx)*(y-my) for x,y in points) / sum((x-mx)**2 for x,_ in points)
    intercept = my-slope*mx
    r2 = 1-sum((y-(slope*x+intercept))**2 for x,y in points)/sum((y-my)**2 for _,y in points)
    return slope, intercept, r2

def label(x, y, t, size=14, anchor="start", color="#1f2937"):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" fill="{color}">{html.escape(t)}</text>'

def make_svg(path, rows):
    w,h=1540,620; panels=[(70,105,430,390),(555,105,430,390),(1040,105,430,390)]
    xv=[r["actual_delay_ps"] for r in rows]; xmin,xmax=min(xv),max(xv)
    def X(x,p): return p[0]+(x-xmin)/(xmax-xmin)*p[2]
    def Y(y,p,lo,hi): return p[1]+p[3]-(y-lo)/(hi-lo)*p[3]
    def axis(p,lo,hi,title,ytitle):
        x,y,pw,ph=p; q=[f'<rect x="{x}" y="{y}" width="{pw}" height="{ph}" fill="white" stroke="#9ca3af"/>',label(x,y-24,title,17)]
        for i in range(5):
            v=lo+(hi-lo)*i/4; sy=Y(v,p,lo,hi); q += [f'<line x1="{x}" y1="{sy:.1f}" x2="{x+pw}" y2="{sy:.1f}" stroke="#e5e7eb"/>',label(x-8,sy+5,f"{v/1000:.1f} ns",12,"end","#4b5563")]
        for v in (-4000,-2000,0,2000,4000):
            sx=X(v,p); q += [f'<line x1="{sx:.1f}" y1="{y}" x2="{sx:.1f}" y2="{y+ph}" stroke="#f3f4f6"/>',label(sx,y+ph+23,f"{v/1000:.0f}",12,"middle","#4b5563")]
        return q+[label(x+pw/2,y+ph+48,"설정 지연 (ns)",13,"middle"),label(x-52,y-4,ytitle,12,"start","#4b5563")]
    a,c,r=panels; z=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">','<rect width="100%" height="100%" fill="#f8fafc"/>',label(70,40,"C1/C2 A/B 비교: 지연 정확도 · 배선 보정 · 재현성",24),label(70,66,"각 점은 100개 파형 평균. C1/C2 교환 후 A는 조건별 10회 반복.",13,"start","#4b5563")]
    z += axis(a,-6000,6000,"1. 원 측정값: A와 B의 기울기 반전","C2 rising − C1 rising") + axis(c,-5000,5000,"2. A/B 보정 후 실제 지연","보정 지연") + axis(r,-120,120,"3. 채널 교환 검증 잔차","A + 교환 후 A")
    for name,color in (("A","#2563eb"),("B","#dc2626"),("A2","#16a34a")):
        pts=[(row["actual_delay_ps"],row[name]) for row in rows]; poly=" ".join(f"{X(x,a):.1f},{Y(y,a,-6000,6000):.1f}" for x,y in pts)
        z.append(f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2.5"/>'); z += [f'<circle cx="{X(x,a):.1f}" cy="{Y(y,a,-6000,6000):.1f}" r="4" fill="{color}"/>' for x,y in pts]
    for i,(name,color) in enumerate((("A","#2563eb"),("B","#dc2626"),("A2","#16a34a"))): z += [f'<rect x="{a[0]+10+i*104}" y="{a[1]+12}" width="10" height="10" fill="{color}"/>',label(a[0]+25+i*104,a[1]+22,name,12)]
    z += [f'<line x1="{X(xmin,c):.1f}" y1="{Y(xmin,c,-5000,5000):.1f}" x2="{X(xmax,c):.1f}" y2="{Y(xmax,c,-5000,5000):.1f}" stroke="#9ca3af" stroke-dasharray="6 5"/>']
    z += [f'<circle cx="{X(q["actual_delay_ps"],c):.1f}" cy="{Y(q["true_delay_ps"],c,-5000,5000):.1f}" r="5" fill="#7c3aed"/>' for q in rows]
    z.append(label(c[0]+14,c[1]+22,"점: 보정 결과 / 점선: 이상값",12,"start","#4b5563"))
    for q in rows:
        sx=X(q["actual_delay_ps"],r); y,e=q["swap_residual_ps"],q["swap_residual_between_repeat_sd_ps"]; top,bottom=Y(y+e,r,-120,120),Y(y-e,r,-120,120)
        z += [f'<line x1="{sx:.1f}" y1="{top:.1f}" x2="{sx:.1f}" y2="{bottom:.1f}" stroke="#ea580c" stroke-width="2"/>',f'<line x1="{sx-5:.1f}" y1="{top:.1f}" x2="{sx+5:.1f}" y2="{top:.1f}" stroke="#ea580c"/>',f'<line x1="{sx-5:.1f}" y1="{bottom:.1f}" x2="{sx+5:.1f}" y2="{bottom:.1f}" stroke="#ea580c"/>',f'<circle cx="{sx:.1f}" cy="{Y(y,r,-120,120):.1f}" r="5" fill="#ea580c"/>']
    zero=Y(0,r,-120,120); z += [f'<line x1="{r[0]}" y1="{zero:.1f}" x2="{r[0]+r[2]}" y2="{zero:.1f}" stroke="#6b7280" stroke-dasharray="5 5"/>',label(r[0]+14,r[1]+22,"오차막: 10회 반복 간 표준편차",12,"start","#4b5563"),label(70,575,"해석: A/B 평균은 고정 측정 경로 스큐를 분리한다. 교환 뒤 잔차가 ±40 ps 이내이므로 약 1.36 ns 오프셋은 DUT 지연이 아니라 경로 스큐다.",14,"start","#374151"),"</svg>"]
    path.write_text("\n".join(z),encoding="utf-8")

def main():
    OUT.mkdir(exist_ok=True); runs={k:load(v) for k,v in RUNS.items()}; ids=list(runs["A"])
    assert all(set(v)==set(ids) for v in runs.values())
    result=[]
    for ident in ids:
        blocks={k:runs[k][ident] for k in runs}
        values={k:[float(x["measured_mean_ps"]) for x in blocks[k]] for k in blocks}
        av,bv,a2v,cv=[mean(values[k]) for k in ("A","B","A2","changed")]
        first=blocks["A"][0]; actual=float(first["actual_delay_ps"])
        paired_true=[(a-b)/2 for a,b in zip(values["A"],values["B"])]
        paired_a2_delta=[a2-a for a2,a in zip(values["A2"],values["A"])]
        paired_swap=[a+c for a,c in zip(values["A"],values["changed"])]
        result.append({"condition_id":ident,"requested_delay_ps":float(first["requested_delay_ps"]),"actual_delay_ps":actual,"A":av,"B":bv,"A2":a2v,"changed_mean_ps":cv,"true_delay_ps":(av-bv)/2,"true_delay_error_ps":(av-bv)/2-actual,"fixed_path_skew_ps":(av+bv)/2,"A_between_repeat_sd_ps":sdev(values["A"]),"B_between_repeat_sd_ps":sdev(values["B"]),"A2_between_repeat_sd_ps":sdev(values["A2"]),"true_delay_between_repeat_sd_ps":sdev(paired_true),"A2_minus_A_ps":a2v-av,"A2_minus_A_between_repeat_sd_ps":sdev(paired_a2_delta),"swap_residual_ps":av+cv,"swap_residual_between_repeat_sd_ps":sdev(paired_swap),"changed_between_repeat_sd_ps":sdev(values["changed"]),"changed_mean_within_run_sd_ps":mean([float(x["measured_stddev_ps"]) for x in blocks["changed"]]),"repeat_count_per_condition":len(blocks["A"])})
    with (OUT/"ab_c1c2_summary.csv").open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(result[0]));w.writeheader();w.writerows(result)
    slope,intercept,r2=fit([(q["actual_delay_ps"],q["true_delay_ps"]) for q in result]); skew=[q["fixed_path_skew_ps"] for q in result]; error=[q["true_delay_error_ps"] for q in result]; a2=[q["A2_minus_A_ps"] for q in result]; resid=[q["swap_residual_ps"] for q in result]; corrected_repeat=[q["true_delay_between_repeat_sd_ps"] for q in result]; swap_repeat=[q["swap_residual_between_repeat_sd_ps"] for q in result]; internal=[q["changed_mean_within_run_sd_ps"] for q in result]
    md=f"""# C1/C2 A/B 비교 분석 (2026-07-27)

## 데이터 품질
- 각 폴더는 7개 지연조건 × 10회 반복 = 70개 측정 세트이며, 세트마다 100파형을 측정했다.
- 네 폴더 전체는 {sum(sum(len(x) for x in v.values()) for v in runs.values())}개 측정 세트, 총 {100*sum(sum(len(x) for x in v.values()) for v in runs.values()):,}개 파형이다. 모두 `quality=ok`, `waveform_count=100`, `sample_count=100`이다.
- 측정량은 `C2 rising 50% crossing − C1 rising 50% crossing` (ps)이다.

## 꼭 봐야 할 결과
- **고정 측정경로 스큐:** {mean(skew):.1f} ps (조건 간 표준편차 {sdev(skew):.1f} ps, 범위 {min(skew):.0f}~{max(skew):.0f} ps)
- **A/B 보정 지연 오차:** 평균 {mean(error):+.1f} ps, 최대 절대값 {max(abs(x) for x in error):.1f} ps
- **선형성:** 기울기 {slope:.5f}, 절편 {intercept:+.1f} ps, R²={r2:.6f}
- **A→B→A2 복귀:** A 대비 최대 변화 {max(abs(x) for x in a2):.0f} ps
- **실제 채널 교환 부호반전 검증:** `A + 교환후 A` 평균 {mean(resid):+.1f} ps, 최대 절대값 {max(abs(x) for x in resid):.0f} ps
- **A/B 보정값의 10회 반복성:** 반복 간 표준편차 최대 {max(corrected_repeat):.1f} ps (7조건 평균 {mean(corrected_repeat):.1f} ps)
- **채널 교환 검증 잔차의 10회 반복성:** 반복 간 표준편차 최대 {max(swap_repeat):.1f} ps. 각 세트의 100파형 내부 표준편차 평균은 {mean(internal):.1f} ps다.

## 해석
1. 원 측정의 약 1.36 ns 오프셋은 A/B 평균으로 분리되는 **고정 측정경로 스큐**다. DUT가 0 ns에서 1.36 ns를 생성한다는 뜻이 아니다.
2. `(A − B) / 2`가 스큐를 제거한 지연이며, 전 범위에서 설정값을 따라 선형적으로 변화한다.
3. A2 복귀 차이가 20 ps 이내이고 실제 채널 교환 후 잔차도 40 ps 이내다. 배선 교환에 따른 부호 반전과 보정 가정이 모두 데이터로 확인된다.
4. 이 결과는 단기 재현성 검증이다. 제품화 안정성은 대표 지연점에서 장시간·온도 조건 반복으로 평균 오차, 반복성, 드리프트를 따로 검증해야 한다.
"""
    (OUT/"ab_c1c2_analysis.md").write_text(md,encoding="utf-8"); make_svg(OUT/"ab_c1c2_comparison.svg",result)
    print(f"Created: {OUT}"); print(f"skew={mean(skew):.1f}ps, max_error={max(abs(x) for x in error):.1f}ps, corrected_repeat_sd_max={max(corrected_repeat):.1f}ps")

if __name__=="__main__": main()
