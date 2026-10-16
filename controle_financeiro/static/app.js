// Utilidades compartilhadas pelos formulários.
window.cf = (() => {
  // Mesmas regras de dinheiro.avaliar: "-609,92/2", "1.234,56", "-175-53,9-(-19,9-10)".
  function avaliar(expr) {
    let t = (expr || "").trim().replace(/^=/, "").replace(/R\$/g, "").replace(/\s/g, "");
    if (t.includes(",")) t = t.replace(/(\d)\.(?=\d{3}(\D|$))/g, "$1").replace(/,/g, ".");
    if (!t) return null;
    let i = 0;
    const num = () => {
      const m = /^\d+(\.\d+)?|^\.\d+/.exec(t.slice(i));
      if (!m) throw 0;
      i += m[0].length;
      return parseFloat(m[0]);
    };
    const fator = () => {
      if (t[i] === "-") { i++; return -fator(); }
      if (t[i] === "+") { i++; return fator(); }
      if (t[i] === "(") { i++; const v = soma(); if (t[i] !== ")") throw 0; i++; return v; }
      return num();
    };
    const termo = () => {
      let v = fator();
      while (t[i] === "*" || t[i] === "/") {
        const op = t[i++], d = fator();
        if (op === "/" && d === 0) throw 0;
        v = op === "*" ? v * d : v / d;
      }
      return v;
    };
    const soma = () => {
      let v = termo();
      while (t[i] === "+" || t[i] === "-") { const op = t[i++]; v = op === "+" ? v + termo() : v - termo(); }
      return v;
    };
    try {
      const v = soma();
      return i === t.length && isFinite(v) ? v : null;
    } catch { return null; }
  }

  // Formato editável, como dinheiro.para_entrada: -304,96
  function formatar(v) {
    const centavos = Math.round(v * 100 + (v >= 0 ? 1e-9 : -1e-9));
    const abs = Math.abs(centavos);
    return (centavos < 0 ? "-" : "") + Math.floor(abs / 100) + "," + String(abs % 100).padStart(2, "0");
  }

  return { avaliar, formatar };
})();

// Seções que podem ser escondidas (ex.: parcelados quitados, fixos encerrados).
document.addEventListener("DOMContentLoaded", () => {
  for (const secao of document.querySelectorAll(".secao-ocultavel")) {
    const chave = secao.dataset.chave;
    const conteudo = secao.querySelector(".secao-conteudo"), mostrar = secao.querySelector(".secao-mostrar");
    const aplicar = (esconder) => { conteudo.hidden = esconder; mostrar.hidden = !esconder; };
    let esconder = false;
    try { esconder = localStorage.getItem(chave) === "1"; } catch {}
    aplicar(esconder);
    secao.addEventListener("click", (ev) => {
      const alvo = ev.target.closest(".secao-esconder, .secao-mostrar");
      if (!alvo) return;
      esconder = alvo.classList.contains("secao-esconder");
      aplicar(esconder);
      try { localStorage.setItem(chave, esconder ? "1" : "0"); } catch {}
    });
  }
});

// Barra de tempo: seletor de mês/ano e menu ⋮.
document.addEventListener("DOMContentLoaded", () => {
  const barra = document.querySelector(".barra-tempo");
  if (!barra) return;
  const input = barra.querySelector(".bt-escolher"), menu = barra.querySelector(".bt-menu");
  const abrir = () => { try { input.showPicker(); } catch { input.focus(); input.click(); } };
  input.closest("label").addEventListener("click", (ev) => { if (ev.target !== input) { ev.preventDefault(); abrir(); } });
  barra.querySelector(".bt-abrir-escolha").addEventListener("click", () => { menu.open = false; abrir(); });
  input.addEventListener("change", () => {
    const [a, m] = input.value.split("-");
    if (!a || !m) return;
    const base = input.dataset.base;
    location.href = input.dataset.destino === "ano" ? `${base}/ano/${+a}` : `${base}/mes/${+a}/${+m}`;
  });
  document.addEventListener("click", (ev) => { if (menu.open && !menu.contains(ev.target)) menu.open = false; });
});
