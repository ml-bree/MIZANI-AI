import { RadialBarChart, RadialBar, PolarAngleAxis } from "recharts"

export default function RatioGauge({ ratio }) {
  const pct = Math.min((ratio / 3) * 100, 100)
  const color = ratio < 1.2 ? "#22c55e" : ratio < 1.5 ? "#eab308" : "#ef4444"

  return (
    <div className="bg-[#2a1f0e] border border-[#8B4513]/40 rounded-lg p-5 flex flex-col items-center">
      <p className="text-xs tracking-widest text-[#8B6914] mb-2">SPEND-PROMISE RATIO</p>
      <RadialBarChart width={180} height={180} cx={90} cy={90}
        innerRadius={55} outerRadius={80} data={[{ value: pct }]} startAngle={180} endAngle={0}>
        <PolarAngleAxis type="number" domain={[0, 100]} angleAxisId={0} tick={false} />
        <RadialBar background={{ fill: "#1a1209" }} dataKey="value" cornerRadius={8}
          fill={color} angleAxisId={0} />
      </RadialBarChart>
      <p className="text-4xl font-bold mt-[-60px]" style={{ color }}>{ratio}</p>
      <p className="text-xs text-[#8B6914] mt-8">SPR INDEX</p>
    </div>
  )
}
