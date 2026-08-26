/* Southend Ticket Stats - front end.
   No framework: one availability fetch, one history fetch, hand-rolled SVG chart. */

(function () {
  "use strict";

  var nf = new Intl.NumberFormat("en-GB");
  var SVG_NS = "http://www.w3.org/2000/svg";

  /* ND, NE and NF are the away end. The server flags them; how many are
     opened depends on the visiting support, so they are named as a set
     wherever the page talks about them. */
  function awayLeaves(nodes) {
    return nodes.filter(function (node) { return node.away; });
  }

  function names(nodes) {
    var list = nodes.map(function (node) { return node.name || node.code; });
    if (list.length < 2) { return list.join(""); }
    return list.slice(0, -1).join(", ") + " and " + list[list.length - 1];
  }

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

    // An away block with no inventory is closed for this fixture, not sold
    // out; the two must not look alike.
    if (!block.in_use) {
      classes.push("block--unused");
    } else if (block.sold_out) {
      classes.push("block--soldout");
    } else if (freeRatio > 0.35) {
      classes.push("block--roomy");
    } else {
      classes.push("block--tight");
    }
    if (block.away) { classes.push("block--away"); }
    div.className = classes.join(" ");

    var top = document.createElement("div");
    top.className = "block__top";

    var name = document.createElement("span");
    name.className = "block__name";
    name.textContent = block.name || block.code;
    if (block.away) {
      var tag = document.createElement("span");
      tag.className = "tag tag--away";
      tag.textContent = "Away";
      name.appendChild(document.createTextNode(" "));
      name.appendChild(tag);
    }

    var count = document.createElement("span");
    count.className = "block__count";
    count.textContent = !block.in_use
      ? "not in use"
      : (block.sold_out ? "sold out" : fmt(block.open) + " left");

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
     are dropped rather than shown as permanently empty rows. The away end is
     the exception: which of ND, NE and NF is opened changes from game to
     game, so a closed one is worth saying out loud. */
  function isListed(node) {
    if (node.in_use || node.away) { return true; }
    return (node.children || []).some(isListed);
  }

  /* Heads the away run, so the blocks are read as a set rather than three
     unrelated cards that happen to sit together. */
  function awayHeading(away) {
    var open = away.filter(function (node) { return node.in_use; });
    var head = document.createElement("div");
    head.className = "tier tier--away";

    var title = document.createElement("span");
    title.textContent = "Away supporters";
    head.appendChild(title);

    var note = document.createElement("span");
    note.className = "tier__note";
    note.textContent = open.length
      ? names(open) + " open for this fixture"
      : "none of it open for this fixture";
    head.appendChild(note);

    return head;
  }

  /* A stand can mix blocks sitting directly beneath it with a whole nested
     tier (West Stand has Q-X alongside Hospitality Level). Runs of leaf
     blocks therefore get their own grid rather than inheriting one from the
     parent, which would otherwise leave them stacked full-width. */
  function appendBlocks(container, nodes) {
    var listed = nodes.filter(isListed);
    var run = null;
    var runIsAway = null;

    function flush() { run = null; runIsAway = null; }

    // The away blocks sit at the end of the North Bank in the club's own
    // ordering, so they break out into their own labelled grid in place.
    function gridFor(node) {
      var away = !!node.away;
      if (run && runIsAway === away) { return run; }
      if (away) { container.appendChild(awayHeading(awayLeaves(listed))); }
      run = document.createElement("div");
      run.className = "blocks";
      runIsAway = away;
      container.appendChild(run);
      return run;
    }

    listed.forEach(function (node) {
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
        gridFor(node).appendChild(blockNode(node));
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
      // Season tickets come back without a total, so there is no "of N" to
      // quote — just say how many are left.
      counts.innerHTML = stand.total
        ? "<strong>" + fmt(stand.open) + "</strong> available of " + fmt(stand.total)
        : "<strong>" + fmt(stand.open) + "</strong> available";

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
    var name = block.name || block.code;
    return block.away ? name + " · away end" : name;
  }

  function blockDetail(block) {
    if (!block.in_use) {
      if (block.away) { return "not in use for this fixture"; }
      return block.has_seats ? "not sold here" : "no seating";
    }
    if (block.sold_out) {
      return "sold out — all " + fmt(block.total) + " seats gone";
    }
    if (!block.total) {
      return fmt(block.open) + " available";
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

        // The bar shows what proportion of the block is left, so it needs a
        // total to divide by. Season tickets have none, and the count alone
        // tells the story there.
        if (block.in_use && !block.sold_out && block.total) {
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
      if (block.away) { node.classList.add("seg--away"); }
      if (node.classList.contains("seg-shape")) { painted++; }

      if (node.nodeName === "g") {
        if (block.away) { tagAwayBlock(node); }
        // aria-label rather than <title>: <title> is what makes the browser
        // show its own tooltip, and that waits about a second before
        // appearing. Screen readers still get a name from aria-label.
        node.setAttribute("aria-label", blockLabel(block) + " — " + blockDetail(block));
        node.setAttribute("tabindex", "0");
      }
    });

    attachTooltip(host, byCode);
    describeMap(blocks);
  }

  /* The club's own plan marks the away end with small print down the side of
     the North Bank, which disappears once the blocks are coloured. Tagging
     each block instead keeps it legible at any size; the tag is drawn from
     the block's own box so it lands under the letter wherever it sits. */
  function tagAwayBlock(group) {
    var box;
    try { box = group.getBBox(); } catch (err) { return; }
    if (!box || box.width <= 0 || box.height <= 0) { return; }

    var tag = document.createElementNS(SVG_NS, "text");
    tag.setAttribute("class", "seg-away-tag");
    tag.setAttribute("x", box.x + box.width / 2);
    tag.setAttribute("y", box.y + box.height * 0.82);
    tag.setAttribute("text-anchor", "middle");
    tag.setAttribute("font-size", Math.round(Math.min(box.height * 0.22, box.width * 0.3)));
    tag.textContent = "AWAY";
    group.appendChild(tag);
  }

  function describeMap(blocks) {
    var caption = el("map-caption");
    if (!caption) { return; }

    var away = awayLeaves(blocks);
    var open = away.filter(function (b) { return b.in_use; });
    // Away blocks are hatched when closed too, so they are explained first;
    // otherwise the same hatch would carry two different meanings unlabelled.
    var unsold = blocks.filter(function (b) {
      return !b.in_use && b.has_seats && !b.away;
    });

    var lines = [];
    if (away.length) {
      var closed = away.filter(function (b) { return !b.in_use; });
      var sentence = "Blocks " + names(away) + " in the North Bank are the away end. ";
      if (!open.length) {
        sentence += "None of it is in use for this fixture.";
      } else if (!closed.length) {
        sentence += "All of it is in use for this fixture.";
      } else {
        sentence += names(open) + (open.length > 1 ? " are" : " is") +
          " in use for this fixture; " + names(closed) +
          (closed.length > 1 ? " are" : " is") + " hatched because " +
          (closed.length > 1 ? "they are" : "it is") + " not.";
      }
      lines.push(sentence);
    }
    if (unsold.length) {
      lines.push(
        "The other hatched blocks hold real seats that aren’t sold through " +
        "the club’s ticketing — directors, press and broadcast: " +
        unsold.map(function (b) { return b.name || b.code; }).join(", ") + "."
      );
    }
    caption.textContent = lines.join(" ");
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

    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Tickets sold over time");

    // horizontal gridlines + y labels
    for (var i = 0; i <= 3; i++) {
      var v = yMin + (yMax - yMin) * (i / 3);
      var y = sy(v);
      var line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("class", "chart__grid");
      line.setAttribute("x1", pad.left); line.setAttribute("x2", W - pad.right);
      line.setAttribute("y1", y); line.setAttribute("y2", y);
      svg.appendChild(line);

      var label = document.createElementNS(SVG_NS, "text");
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

    var area = document.createElementNS(SVG_NS, "path");
    area.setAttribute("class", "chart__area");
    area.setAttribute("d", d + " L" + sx(tMax).toFixed(1) + " " + (pad.top + innerH) +
                           " L" + sx(tMin).toFixed(1) + " " + (pad.top + innerH) + " Z");
    svg.appendChild(area);

    var path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("class", "chart__line");
    path.setAttribute("d", d);
    svg.appendChild(path);

    // end marker with a tooltip
    var dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("class", "chart__dot");
    dot.setAttribute("cx", sx(times[times.length - 1]));
    dot.setAttribute("cy", sy(sold[sold.length - 1]));
    dot.setAttribute("r", 3.5);
    var title = document.createElementNS(SVG_NS, "title");
    title.textContent = nf.format(sold[sold.length - 1]) + " sold";
    dot.appendChild(title);
    svg.appendChild(dot);

    [[tMin, "start"], [tMax, "end"]].forEach(function (pair) {
      var t = document.createElementNS(SVG_NS, "text");
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
