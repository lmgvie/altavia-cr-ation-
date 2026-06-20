const STORAGE_KEY = "paris_sportifs_bets";

function loadBets() {
  return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
}

function saveBets(bets) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(bets));
}

function gainForBet(bet) {
  if (bet.statut === "gagne") return bet.mise * bet.cote;
  if (bet.statut === "rembourse") return bet.mise;
  return 0;
}

function pnlForBet(bet) {
  if (bet.statut === "en_attente") return 0;
  return gainForBet(bet) - bet.mise;
}

function formatEuro(n) {
  return n.toFixed(2).replace(".", ",") + " €";
}

let pnlChart = null;

function renderChart(bets) {
  const sorted = [...bets]
    .filter(b => b.statut !== "en_attente")
    .sort((a, b) => a.date.localeCompare(b.date));

  let cumul = 0;
  const labels = [];
  const data = [];
  sorted.forEach(bet => {
    cumul += pnlForBet(bet);
    labels.push(bet.date);
    data.push(cumul.toFixed(2));
  });

  const ctx = document.getElementById("pnlChart");
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "PnL cumulé (€)",
        data,
        borderColor: "#3d7eff",
        backgroundColor: "rgba(61,126,255,0.15)",
        tension: 0.2,
        fill: true,
        pointRadius: 3
      }]
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: "#e6e6e6" } } },
      scales: {
        x: { ticks: { color: "#9aa0b4" }, grid: { color: "#2a2d3a" } },
        y: { ticks: { color: "#9aa0b4" }, grid: { color: "#2a2d3a" } }
      }
    }
  });
}

function render() {
  const bets = loadBets();
  const tbody = document.getElementById("betBody");
  tbody.innerHTML = "";

  let totalMises = 0, totalGains = 0, totalPnl = 0;

  bets.forEach((bet, index) => {
    const gain = gainForBet(bet);
    const pnl = pnlForBet(bet);

    if (bet.statut !== "en_attente") {
      totalMises += bet.mise;
      totalGains += gain;
      totalPnl += pnl;
    }

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${bet.date}</td>
      <td>${bet.event}</td>
      <td>${bet.pari}</td>
      <td>${formatEuro(bet.mise)}</td>
      <td>${bet.cote.toFixed(2)}</td>
      <td><span class="badge ${bet.statut}">${labelStatut(bet.statut)}</span></td>
      <td>${formatEuro(gain)}</td>
      <td class="${pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : ''}">${formatEuro(pnl)}</td>
      <td><button class="btn-delete" data-index="${index}">&times;</button></td>
    `;
    tbody.appendChild(tr);
  });

  document.getElementById("statMises").textContent = formatEuro(totalMises);
  document.getElementById("statGains").textContent = formatEuro(totalGains);

  const pnlEl = document.getElementById("statPnl");
  pnlEl.textContent = formatEuro(totalPnl);
  pnlEl.className = "stat-value " + (totalPnl > 0 ? "positive" : totalPnl < 0 ? "negative" : "");

  const roi = totalMises > 0 ? (totalPnl / totalMises) * 100 : 0;
  const roiEl = document.getElementById("statRoi");
  roiEl.textContent = roi.toFixed(1) + " %";
  roiEl.className = "stat-value " + (roi > 0 ? "positive" : roi < 0 ? "negative" : "");

  renderChart(bets);

  document.querySelectorAll(".btn-delete").forEach(btn => {
    btn.addEventListener("click", () => {
      const bets = loadBets();
      bets.splice(Number(btn.dataset.index), 1);
      saveBets(bets);
      render();
    });
  });
}

function labelStatut(statut) {
  return {
    en_attente: "En attente",
    gagne: "Gagné",
    perdu: "Perdu",
    rembourse: "Remboursé"
  }[statut] || statut;
}

document.getElementById("betForm").addEventListener("submit", (e) => {
  e.preventDefault();

  const bet = {
    date: document.getElementById("date").value,
    event: document.getElementById("event").value,
    pari: document.getElementById("pari").value,
    mise: parseFloat(document.getElementById("mise").value),
    cote: parseFloat(document.getElementById("cote").value),
    statut: document.getElementById("statut").value
  };

  const bets = loadBets();
  bets.push(bet);
  saveBets(bets);

  e.target.reset();
  render();
});

render();
