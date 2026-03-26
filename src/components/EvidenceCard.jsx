export default function EvidenceCard({ item }) {
  const pct = Math.round(item.confidence * 100)
  return (
    <div className="bg-[#2a1f0e] border border-[#8B4513]/40 rounded-lg p-4 mb-3">
      <div className="flex justify-between items-center mb-2">
        <span className="text-sm font-bold text-[#f0e6d3]">{item.label}</span>
        <span className="text-[#E2711D] font-bold text-sm">KSh {(item.value/1e6).toFixed(1)}M</span>
      </div>
      <div className="flex items-center gap-3">
        <div className="flex-1 h-1.5 bg-[#1a1209] rounded-full">
          <div className="h-1.5 bg-[#E2711D] rounded-full" style={{ width: `${pct}%` }} />
        </div>
        <span className="text-xs text-[#8B6914]">{pct}% confidence</span>
      </div>
    </div>
  )
}
