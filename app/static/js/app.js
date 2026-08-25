/* Southend Ticket Stats - front end.
   No framework: one availability fetch, one history fetch, hand-rolled SVG chart. */

(function () {
  "use strict";

  var nf = new Intl.NumberFormat("en-GB");

  function el(id) { return document.getElementById(id); }
  function fmt(n) { return (n === null || n === undefined) ? "—" : nf.format(n); }

  /* --- fixture switcher --- */

  var picker = el("fixture");
  if (picker) {
    picker.addEventListener("change", function () {
      window.location.href = "/" + this.value;
    });
  }

  var hero = document.querySelector(".hero");
  if (!hero) { return; }
  var code = hero.dataset.code;

  /* --- live availability --- */

  var status = el("status");

  function setStatus(kind, message) {
    status.className = "status status--" + kind;
    status.textContent = message;
  }

  function renderTotals(data, soldOutBlocks) {
    var t = data.totals;
    el("stat-sold").textContent = fmt(t.sold);
    el("stat-available").textContent = fmt(t.available);
    el("stat-capacity").textContent = fmt(t.capacity);
    el("stat-soldout").textContent = fmt(soldOutBlocks);
    el("stat-sold-pct").textContent = t.percent_sold + "% of seats in use";
    el("stats").hidden = false;

    el("meter-fill").style.width = Math.min(100, t.percent_sold) + "%";
    el("meter-caption").textContent =
      fmt(t.sold) + " of " + fmt(t.capacity) + " seats have gone · " +
      fmt(t.available) + " still available";

    var verify = el("seat-verify");
    var seats = data.seats || {};
    if (seats.verified === true) {
      verify.textContent = "✓ " + fmt(seats.total) + " seats read individually";
      verify.style.color = "var(--free)";
    } else if (seats.verified === false) {
      verify.textContent = "⚠ seat map didn’t reconcile — counts may be approximate";
      verify.style.color = "#c0392b";
    } else {
      verify.textContent = "";
    }
    el("meter").hidden = false;
  }

  /* The bar shows how much of the block is still available, matching the
     "N left" label beside it, and is coloured by scarcity. Showing sold
     proportion instead would invert the meaning of the colour. */
  function blockNode(block) {
    var div = document.createElement("div");
    var freeRatio = block.total ? block.open / block.total : 0;
    var classes = ["block"];

    if (block.sold_out) {
      classes.push("block--soldout");
    } else if (freeRatio > 0.35) {
      classes.push("block--roomy");
    } else {
      classes.push("block--tight");
    }
    div.className = classes.join(" ");

    var top = document.createElement("div");
    top.className = "block__top";

    var name = document.createElement("span");
    name.className = "block__name";
    name.textContent = block.name || block.code;

    var count = document.createElement("span");
    count.className = "block__count";
    count.textContent = block.sold_out ? "sold out" : fmt(block.open) + " left";

    top.appendChild(name);
    top.appendChild(count);
    div.appendChild(top);

    if (block.total) {
      var bar = document.createElement("div");
      bar.className = "block__bar";
      bar.setAttribute("role", "img");
      bar.setAttribute(
        "aria-label",
        fmt(block.open) + " of " + fmt(block.total) + " seats available"
      );
      var fill = document.createElement("span");
      fill.className = "block__fill";
      // Keep a sliver visible so "almost gone" still reads as non-zero.
      fill.style.width =
        (block.open ? Math.max(2, Math.round(freeRatio * 100)) : 0) + "%";
      bar.appendChild(fill);
      div.appendChild(bar);

      div.title =
        fmt(block.open) + " available of " + fmt(block.total) +
        " · " + fmt(block.sold) + " sold";
    }
    return div;
  }

  /* Blocks carrying no inventory are never sold through this system, so they
     are dropped rather than shown as permanently empty rows. */
  function hasSeats(node) {
    if (node.in_use) { return true; }
    return (node.children || []).some(hasSeats);
  }

  /* A stand can mix blocks sitting directly beneath it with a whole nested
     tier (West Stand has Q-X alongside Hospitality Level). Runs of leaf
     blocks therefore get their own grid rather than inheriting one from the
     parent, which would otherwise leave them stacked full-width. */
  function appendBlocks(container, nodes) {
    var run = null;

    function flush() { run = null; }

    nodes.filter(hasSeats).forEach(function (node) {
      if (node.children && node.children.length) {
        flush();
        var tier = document.createElement("div");
        tier.className = "tier";
        tier.textContent = node.name;
        container.appendChild(tier);

        var nested = document.createElement("div");
        appendBlocks(nested, node.children);
        container.appendChild(nested);
      } else {
        if (!run) {
          run = document.createElement("div");
          run.className = "blocks";
          container.appendChild(run);
        }
        run.appendChild(blockNode(node));
      }
    });
  }

  function countSoldOut(nodes) {
    return nodes.reduce(function (total, node) {
      if (node.children && node.children.length) {
        return total + countSoldOut(node.children);
      }
      return total + (node.sold_out ? 1 : 0);
    }, 0);
  }

  function renderStands(stands) {
    var host = el("stands");
    host.textContent = "";

    stands.forEach(function (stand) {
      var section = document.createElement("section");
      section.className = "stand";

      var head = document.createElement("div");
      head.className = "stand__head";

      var name = document.createElement("span");
      name.className = "stand__name";
      name.textContent = stand.name || stand.code;

      var counts = document.createElement("span");
      counts.className = "stand__counts";
      counts.innerHTML = "<strong>" + fmt(stand.open) + "</strong> available of " + fmt(stand.total);

      head.appendChild(name);
      head.appendChild(counts);
      section.appendChild(head);

      var body = document.createElement("div");
      body.className = "stand__body";
      appendBlocks(body, stand.children.length ? stand.children : [stand]);
      section.appendChild(body);

      host.appendChild(section);
    });
  }

  fetch("/api/" + encodeURIComponent(code) + "/latest")
    .then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) { throw new Error(body.error || "Request failed"); }
        return body;
      });
    })
    .then(function (data) {
      renderTotals(data, countSoldOut(data.stands));
      renderStands(data.stands);
      loadMap(data.stands);
      setStatus("ok", "Live from the club’s ticketing system at " + data.retrieved_at + " UTC.");
      loadHistory();
      loadPrices();
    })
    .catch(function (err) {
      setStatus("error", "Couldn’t read live availability: " + err.message);
    });

  /* --- stadium map --- */

  function flatten(nodes, into) {
    into = into || [];
    nodes.forEach(function (node) {
      if (node.children && node.children.length) {
        flatten(node.children, into);
      } else {
        into.push(node);
      }
    });
    return into;
  }

  function loadMap(stands) {
    var host = el("stadium-map");
    if (!host) { return; }

    fetch("/map.svg")
      .then(function (r) {
        if (!r.ok) { throw new Error("map unavailable"); }
        return r.text();
      })
      .then(function (markup) {
        // Parsed as a document rather than assigned to innerHTML, so nothing
        // in the third-party file is evaluated on the way in.
        var doc = new DOMParser().parseFromString(markup, "image/svg+xml");
        var svg = doc.documentElement;
        if (!svg || svg.nodeName === "parsererror") { throw new Error("map did not parse"); }

        host.appendChild(document.importNode(svg, true));
        paintMap(flatten(stands));
      })
      .catch(function () {
        // The block list below already carries the same numbers.
        var panel = el("map-panel");
        if (panel) { panel.hidden = true; }
      });
  }

  function blockLabel(block) {
    return block.name || block.code;
  }

  function blockDetail(block) {
    if (!block.in_use) {
      return block.has_seats ? "not sold here" : "no seating";
    }
    if (block.sold_out) {
      return "sold out — all " + fmt(block.total) + " seats gone";
    }
    return fmt(block.open) + " of " + fmt(block.total) + " available";
  }

  /* Follows the pointer and appears on the first mousemove, where the native
     <title> tooltip would sit idle for about a second first. */
  function attachTooltip(host, byCode) {
    var tip = document.createElement("div");
    tip.className = "map-tip";
    tip.hidden = true;
    host.appendChild(tip);

    var current = null;

    function hide() {
      current = null;
      tip.hidden = true;
    }

    function show(block, event) {
      if (block !== current) {
        current = block;
        tip.textContent = "";

        var name = document.createElement("strong");
        name.textContent = blockLabel(block);
        tip.appendChild(name);

        var detail = document.createElement("span");
        detail.textContent = blockDetail(block);
        tip.appendChild(detail);

        if (block.in_use && !block.sold_out) {
          var bar = document.createElement("span");
          bar.className = "map-tip__bar";
          var fill = document.createElement("i");
          fill.style.width = Math.max(2, Math.round(block.open / block.total * 100)) + "%";
          bar.appendChild(fill);
          tip.appendChild(bar);
        }
        tip.hidden = false;
      }
      position(event);
    }

    function position(event) {
      var box = host.getBoundingClientRect();
      var x = event.clientX - box.left;
      var y = event.clientY - box.top;
      // Keep it inside the map, and above the cursor rather than under it.
      var w = tip.offsetWidth, h = tip.offsetHeight;
      var left = Math.min(Math.max(x - w / 2, 4), Math.max(4, box.width - w - 4));
      var top = y - h - 14;
      if (top < 4) { top = y + 20; }
      tip.style.transform = "translate(" + Math.round(left) + "px," + Math.round(top) + "px)";
    }

    function blockFrom(target) {
      var node = target && target.closest ? target.closest("[data-code]") : null;
      return node ? byCode[node.getAttribute("data-code")] : null;
    }

    host.addEventListener("mousemove", function (event) {
      var block = blockFrom(event.target);
      if (block) { show(block, event); } else { hide(); }
    });
    host.addEventListener("mouseleave", hide);

    // Touch and keyboard both need a way in; neither gets a mousemove.
    host.addEventListener("click", function (event) {
      var block = blockFrom(event.target);
      if (block) { show(block, event); } else { hide(); }
    });
    host.addEventListener("focusin", function (event) {
      var block = blockFrom(event.target);
      if (!block) { return; }
      var box = event.target.getBoundingClientRect();
      show(block, { clientX: box.left + box.width / 2, clientY: box.top + box.height / 2 });
    });
    host.addEventListener("focusout", hide);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { hide(); }
    });
  }

  function paintMap(blocks) {
    var host = el("stadium-map");
    var byCode = {};
    blocks.forEach(function (b) { byCode[b.code] = b; });

    var painted = 0;
    host.querySelectorAll("[data-code]").forEach(function (node) {
      var block = byCode[node.getAttribute("data-code")];
      if (!block) { return; }

      // Groups carry the state for hit-testing; shapes carry it for fill.
      node.classList.add("seg--" + block.state);
      if (node.classList.contains("seg-shape")) { painted++; }

      if (node.nodeName === "g") {
        // aria-label rather than <title>: <title> is what makes the browser
        // show its own tooltip, and that waits about a second before
        // appearing. Screen readers still get a name from aria-label.
        node.setAttribute("aria-label", blockLabel(block) + " — " + blockDetail(block));
        node.setAttribute("tabindex", "0");
      }
    });

    attachTooltip(host, byCode);

    var unsold = blocks.filter(function (b) { return !b.in_use && b.has_seats; });
    var caption = el("map-caption");
    if (caption && unsold.length) {
      caption.textContent =
        "Hatched blocks hold real seats that aren’t sold through the club’s " +
        "ticketing — away allocation, directors, press and broadcast: " +
        unsold.map(function (b) { return b.name || b.code; }).join(", ") + ".";
    }
  }

  /* --- prices --- */

  var gbp = new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" });

  function loadPrices() {
    fetch("/api/" + encodeURIComponent(code) + "/prices")
      .then(function (r) { return r.json(); })
      .then(renderPrices)
      .catch(function () { /* prices are a nice-to-have; stay quiet */ });
  }

  function renderPrices(prices) {
    if (!prices || !prices.length) { return; }
    var host = el("prices");
    host.textContent = "";

    prices.forEach(function (item) {
      var row = document.createElement("tr");

      var th = document.createElement("th");
      th.scope = "row";
      th.appendChild(document.createTextNode(item.type));

      // Restrictions and area limits are the only per-ticket detail left now
      // that the per-area breakdown has gone.
      var notes = [];
      if (item.restriction) { notes.push(item.restriction); }
      if (item.areas) { notes.push(item.areas); }
      if (notes.length) {
        var note = document.createElement("span");
        note.className = "price-note";
        note.textContent = notes.join(" · ");
        th.appendChild(note);
      }

      var td = document.createElement("td");
      if (item.amount === null || item.amount === undefined) {
        td.textContent = "—";
      } else if (item.varies) {
        td.textContent = gbp.format(item.amount) + "–" + gbp.format(item.max_amount);
      } else {
        td.textContent = gbp.format(item.amount);
      }

      row.appendChild(th);
      row.appendChild(td);
      host.appendChild(row);
    });

    el("prices-panel").hidden = false;
  }

  /* --- history chart --- */

  function loadHistory() {
    fetch("/api/" + encodeURIComponent(code) + "/historic")
      .then(function (r) { return r.json(); })
      .then(drawChart)
      .catch(function () {
        el("chart").innerHTML = '<p class="chart__empty">Chart unavailable.</p>';
      });
  }

  function drawChart(points) {
    var host = el("chart");
    host.textContent = "";

    if (!points || points.length < 2) {
      var p = document.createElement("p");
      p.className = "chart__empty";
      p.textContent = points && points.length === 1
        ? "Only one reading so far — the trend line appears once there are a few more."
        : "No readings recorded yet. Check back shortly.";
      host.appendChild(p);
      return;
    }

    var W = host.clientWidth || 800, H = 240;
    var pad = { top: 12, right: 12, bottom: 24, left: 52 };
    var innerW = W - pad.left - pad.right;
    var innerH = H - pad.top - pad.bottom;

    var times = points.map(function (d) { return new Date(d.t).getTime(); });
    var sold = points.map(function (d) { return d.sold; });

    var tMin = Math.min.apply(null, times), tMax = Math.max.apply(null, times);
    var yMin = Math.min.apply(null, sold), yMax = Math.max.apply(null, sold);
    // A flat series would collapse to a zero-height axis; give it headroom.
    if (yMax === yMin) { yMax = yMin + Math.max(1, Math.round(yMin * 0.02)); }
    var yPad = Math.round((yMax - yMin) * 0.12) || 1;
    yMin = Math.max(0, yMin - yPad);
    yMax = yMax + yPad;

    function sx(t) { return tMax === tMin ? pad.left + innerW / 2 : pad.left + (t - tMin) / (tMax - tMin) * innerW; }
    function sy(v) { return pad.top + innerH - (v - yMin) / (yMax - yMin) * innerH; }

    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Tickets sold over time");

    // horizontal gridlines + y labels
    for (var i = 0; i <= 3; i++) {
      var v = yMin + (yMax - yMin) * (i / 3);
      var y = sy(v);
      var line = document.createElementNS(NS, "line");
      line.setAttribute("class", "chart__grid");
      line.setAttribute("x1", pad.left); line.setAttribute("x2", W - pad.right);
      line.setAttribute("y1", y); line.setAttribute("y2", y);
      svg.appendChild(line);

      var label = document.createElementNS(NS, "text");
      label.setAttribute("class", "chart__label");
      label.setAttribute("x", pad.left - 8);
      label.setAttribute("y", y + 4);
      label.setAttribute("text-anchor", "end");
      label.textContent = nf.format(Math.round(v));
      svg.appendChild(label);
    }

    var d = points.map(function (pt, idx) {
      return (idx ? "L" : "M") + sx(times[idx]).toFixed(1) + " " + sy(sold[idx]).toFixed(1);
    }).join(" ");

    var area = document.createElementNS(NS, "path");
    area.setAttribute("class", "chart__area");
    area.setAttribute("d", d + " L" + sx(tMax).toFixed(1) + " " + (pad.top + innerH) +
                           " L" + sx(tMin).toFixed(1) + " " + (pad.top + innerH) + " Z");
    svg.appendChild(area);

    var path = document.createElementNS(NS, "path");
    path.setAttribute("class", "chart__line");
    path.setAttribute("d", d);
    svg.appendChild(path);

    // end marker with a tooltip
    var dot = document.createElementNS(NS, "circle");
    dot.setAttribute("class", "chart__dot");
    dot.setAttribute("cx", sx(times[times.length - 1]));
    dot.setAttribute("cy", sy(sold[sold.length - 1]));
    dot.setAttribute("r", 3.5);
    var title = document.createElementNS(NS, "title");
    title.textContent = nf.format(sold[sold.length - 1]) + " sold";
    dot.appendChild(title);
    svg.appendChild(dot);

    [[tMin, "start"], [tMax, "end"]].forEach(function (pair) {
      var t = document.createElementNS(NS, "text");
      t.setAttribute("class", "chart__label");
      t.setAttribute("x", sx(pair[0]));
      t.setAttribute("y", H - 6);
      t.setAttribute("text-anchor", pair[1]);
      t.textContent = new Date(pair[0]).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
      svg.appendChild(t);
    });

    host.appendChild(svg);
  }
})();
