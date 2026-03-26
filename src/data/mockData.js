export const candidates = {
  "john_kamau_starehe": {
    name: "John Kamau",
    constituency: "Starehe, Nairobi",
    party: "UDA",
    declared_wealth: 4500000,
    declared_budget: 2000000,
    estimated_spend: 8200000,
    ratio: 1.82,
    alert_level: "CRITICAL",
    evidence: [
      { type: "meta_ads", label: "Meta/Facebook Ads", value: 2300000, confidence: 0.89 },
      { type: "crowdsourced", label: "Citizen Reports", value: 1200000, confidence: 0.72 },
      { type: "procurement", label: "Procurement Links", value: 4700000, confidence: 0.85 },
    ],
    reports: [
      { time: "2 min ago", location: "Starehe Market", text: "Large convoy of 15 vehicles spotted, branded merchandise being distributed" },
      { time: "11 min ago", location: "Pangani", text: "Billboard installation crew working on new 48x12ft hoarding" },
      { time: "23 min ago", location: "Ngara", text: "Cash handouts reported at community meeting, approx 500 attendees" },
    ]
  }
}
