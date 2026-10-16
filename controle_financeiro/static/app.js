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

// Textos de ajuda embaixo dos campos viram uma dica (ⓘ) ao lado do rótulo,
// para economizar espaço. Mouse: aparece ao passar; toque: ao tocar no ícone.
document.addEventListener("DOMContentLoaded", () => {
  let n = 0;
  for (const ajuda of document.querySelectorAll("div.ajuda, span.ajuda")) {
    const campo = ajuda.parentElement;
    const rotulos = [...campo.children].filter((el) => el.tagName === "LABEL");
    if (!rotulos.length) continue;  // ajuda de página/seção: continua visível
    // rótulo mais próximo antes do texto (ex.: a caixa "Alterar o valor"), senão o primeiro
    const antes = rotulos.filter((l) => l.compareDocumentPosition(ajuda) & Node.DOCUMENT_POSITION_FOLLOWING);
    const rotulo = antes.length ? antes[antes.length - 1] : rotulos[0];

    const id = `dica-${++n}`;
    const dica = document.createElement("span");
    dica.className = "dica";
    dica.tabIndex = 0;
    dica.setAttribute("role", "button");
    dica.setAttribute("aria-label", "Ajuda");
    dica.setAttribute("aria-describedby", id);
    dica.textContent = "i";
    const texto = document.createElement("span");
    texto.className = "dica-texto";
    texto.id = id;
    texto.setAttribute("role", "tooltip");
    texto.innerHTML = ajuda.innerHTML.replace(/^\s*—\s*/, "");
    dica.appendChild(texto);
    rotulo.appendChild(dica);
    ajuda.remove();
  }

  const ajustar = (dica) => {
    // mantém a caixa dentro da tela
    const t = dica.querySelector(".dica-texto");
    t.style.left = "0px";
    const r = t.getBoundingClientRect(), margem = 8;
    const largura = document.documentElement.clientWidth;  // innerWidth pode incluir área fora da página
    let desloc = 0;
    if (r.right > largura - margem) desloc = largura - margem - r.right;
    if (r.left + desloc < margem) desloc = margem - r.left;
    t.style.left = `${desloc}px`;
  };
  document.addEventListener("mouseover", (ev) => { const d = ev.target.closest(".dica"); if (d) ajustar(d); });
  document.addEventListener("focusin", (ev) => { const d = ev.target.closest(".dica"); if (d) ajustar(d); });
  document.addEventListener("click", (ev) => {
    const d = ev.target.closest(".dica");
    for (const aberta of document.querySelectorAll(".dica.aberta")) if (aberta !== d) aberta.classList.remove("aberta");
    if (!d) return;
    ev.preventDefault();  // não marca a caixa do rótulo
    ev.stopPropagation();
    d.classList.toggle("aberta");
    ajustar(d);
  }, true);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") for (const d of document.querySelectorAll(".dica.aberta")) d.classList.remove("aberta");
  });
});
