import { useState } from "react"
import { motion, AnimatePresence } from "framer-motion"
import { Search, AlertTriangle, Shield, TrendingUp } from "lucide-react"
import { candidates } from "./data/mockData"
import RatioGauge from "./components/RatioGauge"
import EvidenceCard from "./components/EvidenceCard"
import LiveFeed from "./components/LiveFeed"

export default function App() {
  const [query, setQuery] = useState("")
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)

  const handleSearch = () => {
    setLoading(true)
    setResult(null)
    setTimeout(() => {
      setResult(candidates["john_kamau_starehe"])
      setLoading(false)
    }, 1800)
  }

  return (
    <div className="min-h-screen bg-[#1a1209] text-[#f0e6d3] font-mono">
      <header className="border-b border-[#8B4513]/30 px-8 py-5 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-full bg-[#E2711D] flex items-center justify-center">
            <Shield size={16} className="text-white" />
          </div>
          <span className="text-xl font-bold tracking-widest text-[#E2711D]">MIZANI</span>
          <span className="text-xs text-[#8B6914] ml-2 tracking-wider">CAMPAIGN TRANSPARENCY ENGINE</span>
        </div>
        <div className="text-xs text-[#8B6914]">KENYA 2027 ELECTION MONITOR</div>
      </header>

      <div className="max-w-3xl mx-auto mt-16 px-6">
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}>
          <h1 className="text-4xl font-bold text-center mb-2 tracking-tight">
            Follow the <span className="text-[#E2711D]">Money.</span>
          </h1>
          <p className="text-center text-[#8B6914] mb-10 text-sm tracking-wider">
            ENTER A CANDIDATE NAME OR CONSTITUENCY TO BEGIN ANALYSIS
          </p>
          <div className="flex gap-3">
            <input
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleSearch()}
              placeholder="e.g. John Kamau Starehe"
              className="flex-1 bg-[#2a1f0e] border border-[#8B4513]/50 rounded px-5 py-3 text-[#f0e6d3] placeholder-[#5a4a30] focus:outline-none focus:border-[#E2711D] text-sm"
            />
            <button
              onClick={handleSearch}
              className="bg-[#E2711D] hover:bg-[#c45e0f] px-6 py-3 rounded font-bold text-white flex items-center gap-2 transition-colors"
            >
              <Search size={16} /> ANALYZE
            </button>
          </div>
        </motion.div>
      </div>

      <AnimatePresence>
        {loading && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            className="text-center mt-16 text-[#8B6914] text-sm tracking-widest">
            <div className="inline-block w-6 h-6 border-2 border-[#E2711D] border-t-transparent rounded-full animate-spin mb-4" />
            <p>QUERYING IEBC RECORDS... META AD LIBRARY... CITIZEN REPORTS...</p>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {result && (
          <motion.div initial={{ opacity: 0, y: 30 }} animate={{ opacity: 1, y: 0 }}
            className="max-w-5xl mx-auto mt-12 px-6 pb-20">
            <div className="bg-red-900/40 border border-red-500/60 rounded-lg px-6 py-4 mb-8 flex items-center gap-4">
              <AlertTriangle className="text-red-400 shrink-0" size={24} />
              <div>
                <p className="font-bold text-red-300 tracking-wider">CRITICAL ALERT SPR: {result.ratio}</p>
                <p className="text-red-400/80 text-sm mt-1">
                  {result.name} ({result.constituency}) has estimated spend of
                  <strong> {(result.estimated_spend / 1e6).toFixed(1)}M KSh</strong> against declared means of
                  <strong> {(result.declared_wealth / 1e6).toFixed(1)}M KSh</strong>
                </p>
              </div>
            </div>

            <div className="grid grid-cols-3 gap-6 mb-6">
              <div className="col-span-1">
                <RatioGauge ratio={result.ratio} />
              </div>
              <div className="col-span-2 grid grid-cols-2 gap-4">
                {[
                  { label: "DECLARED WEALTH", value: "KSh " + (result.declared_wealth/1e6).toFixed(1) + "M", color: "text-green-400" },
                  { label: "ESTIMATED SPEND", value: "KSh " + (result.estimated_spend/1e6).toFixed(1) + "M", color: "text-red-400" },
                  { label: "DECLARED BUDGET", value: "KSh " + (result.declared_budget/1e6).toFixed(1) + "M", color: "text-yellow-400" },
                  { label: "OVERSPEND", value: "+" + (((result.estimated_spend - result.declared_wealth) / result.declared_wealth) * 100).toFixed(0) + "%", color: "text-red-400" },
                ].map(stat => (
                  <div key={stat.label} className="bg-[#2a1f0e] border border-[#8B4513]/40 rounded-lg p-5">
                    <p className="text-[#8B6914] text-xs tracking-widest mb-2">{stat.label}</p>
                    <p className={"text-2xl font-bold " + stat.color}>{stat.value}</p>
                  </div>
                ))}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-6">
              <div>
                <p className="text-xs tracking-widest text-[#8B6914] mb-4 flex items-center gap-2">
                  <TrendingUp size={12} /> EVIDENCE CHANNELS
                </p>
                {result.evidence.map(e => (
                  <EvidenceCard key={e.type} item={e} />
                ))}
              </div>
              <LiveFeed reports={result.reports} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
