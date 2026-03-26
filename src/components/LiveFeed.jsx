import { useEffect, useState } from "react"
import { motion, AnimatePresence } from "framer-motion"
import { Radio } from "lucide-react"

export default function LiveFeed({ reports }) {
  const [feed, setFeed] = useState(reports)

  useEffect(() => {
    const interval = setInterval(() => {
      setFeed(prev => [{
        time: "just now",
        location: ["CBD", "Eastleigh", "Huruma"][Math.floor(Math.random() * 3)],
        text: "New citizen report received via USSD *384#"
      }, ...prev.slice(0, 4)])
    }, 8000)
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="bg-[#2a1f0e] border border-[#8B4513]/40 rounded-lg p-5">
      <p className="text-xs tracking-widest text-[#8B6914] mb-4 flex items-center gap-2">
        <Radio size={12} className="text-red-400 animate-pulse" /> LIVE CITIZEN REPORTS
      </p>
      <AnimatePresence>
        {feed.map((r, i) => (
          <motion.div key={r.time + i} initial={{ opacity: 0, x: -10 }} animate={{ opacity: 1, x: 0 }}
            className="border-l-2 border-[#E2711D]/40 pl-3 mb-4">
            <p className="text-xs text-[#8B6914]">{r.time} · {r.location}</p>
            <p className="text-sm text-[#f0e6d3] mt-1">{r.text}</p>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  )
}
