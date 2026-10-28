/* 原创像素小人（Kairosoft 风格参考：大头小身/粗描边/亮配色/职业道具）
 * 不包含/不搬运任何开罗素材；所有像素由本文件代码原生绘制。
 * 用法：PixelSprites.svg('host', 96, 'idle'|'walk0'|'walk1'|'blink'|'talk'|'happy'|'think'|'surprised')
 * viewBox 20x26 像素网格；舞台/货架/详情共用。
 */
(function () {
  const OUT = '#2b2b3c';
  const SKIN = '#f7c9a3';
  const SKIN2 = '#e8b088';
  const WHITE = '#ffffff';
  const MOUTH = '#b45309';
  const BLUSH = '#f2a58f';

  const PAL = {
    host:     { hair: '#26324e', hair2: '#1b2438', shirt: '#2563eb', shirt2: '#1e3a8a', pants: '#1e3a8a', accent: '#22d3ee', prop: 'headset' },
    analyst:  { hair: '#3a2b1f', hair2: '#2a1f16', shirt: '#0f766e', shirt2: '#0b5f59', pants: '#0b5f59', accent: '#fef3c7', prop: 'tablet', glasses: true },
    attr:     { hair: '#3f2a6b', hair2: '#2d1d4d', shirt: '#7c3aed', shirt2: '#5b21b6', pants: '#4c1d95', accent: '#c4b5fd', prop: 'magnifier', ahoge: true },
    biz:      { hair: '#4a3320', hair2: '#33220f', shirt: '#b45309', shirt2: '#92400e', pants: '#7c2d12', accent: '#fbbf24', prop: 'helmet' },
    qa:       { hair: '#111827', hair2: '#0b1220', shirt: '#334155', shirt2: '#1f2937', pants: '#1f2937', accent: '#22c55e', prop: 'check' },
    data:     { hair: '#1f2937', hair2: '#111827', shirt: '#0e7490', shirt2: '#155e75', pants: '#155e75', accent: '#cffafe', prop: 'box' },
    content:  { hair: '#334155', hair2: '#1e293b', shirt: '#0f766e', shirt2: '#115e59', pants: '#134e4a', accent: '#a5f3fc', prop: 'notebook' },
    media:    { hair: '#4c0519', hair2: '#881337', shirt: '#be123c', shirt2: '#9f1239', pants: '#881337', accent: '#fda4af', prop: 'megaphone' },
    cost:     { hair: '#64748b', hair2: '#475569', shirt: '#94a3b8', shirt2: '#64748b', pants: '#64748b', accent: '#cbd5e1', prop: 'none', grey: true }
  };

  function svg(id, width, frame) {
    frame = frame || 'idle';
    const p = PAL[id] || PAL.content;
    const parts = [];
    const R = (x, y, w, h, c) => parts.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${c}"/>`);

    // shadow seat
    R(4, 24, 12, 1, '#00000022');

    // legs (walk frames swap)
    const walkA = frame === 'walk0';
    const walkB = frame === 'walk1';
    if (walkA) {
      R(6, 20, 3, 5, OUT); R(12, 19, 3, 5, OUT);
      R(6, 20, 2, 4, p.pants); R(12, 19, 2, 4, p.pants);
      R(5, 24, 4, 1, '#1f2937'); R(12, 23, 4, 1, '#1f2937');
    } else if (walkB) {
      R(8, 19, 3, 5, OUT); R(11, 20, 3, 5, OUT);
      R(8, 19, 2, 4, p.pants); R(11, 20, 2, 4, p.pants);
      R(7, 23, 4, 1, '#1f2937'); R(11, 24, 4, 1, '#1f2937');
    } else {
      R(7, 20, 3, 5, OUT); R(11, 20, 3, 5, OUT);
      R(7, 20, 2, 4, p.pants); R(11, 20, 2, 4, p.pants);
      R(6, 24, 4, 1, '#1f2937'); R(11, 24, 4, 1, '#1f2937');
    }

    // body
    R(5, 13, 10, 8, OUT);
    R(6, 14, 8, 6, p.shirt);
    R(8, 15, 4, 4, p.shirt2);
    R(6, 14, 8, 1, p.shirt2);
    // arms (walk swings)
    if (walkA) {
      R(3, 16, 2, 5, OUT); R(15, 14, 2, 5, OUT);
      R(3, 17, 1, 3, p.shirt); R(16, 15, 1, 3, p.shirt);
      R(3, 21, 2, 1, SKIN); R(15, 19, 2, 1, SKIN);
    } else if (walkB) {
      R(3, 14, 2, 5, OUT); R(15, 16, 2, 5, OUT);
      R(3, 15, 1, 3, p.shirt); R(16, 17, 1, 3, p.shirt);
      R(3, 19, 2, 1, SKIN); R(15, 21, 2, 1, SKIN);
    } else {
      R(3, 15, 2, 6, OUT); R(15, 15, 2, 6, OUT);
      R(3, 16, 1, 4, p.shirt); R(16, 16, 1, 4, p.shirt);
      R(3, 20, 2, 1, SKIN); R(15, 20, 2, 1, SKIN);
    }

    // neck
    R(9, 11, 2, 2, SKIN2);

    // head outline + skin
    R(2, 1, 16, 13, OUT);
    R(3, 2, 14, 11, SKIN);
    R(3, 11, 14, 2, SKIN2);
    // ears
    R(1, 6, 2, 3, OUT); R(1, 7, 2, 2, SKIN2);
    R(17, 6, 2, 3, OUT); R(17, 7, 2, 2, SKIN2);

    // hair base
    if (p.prop !== 'helmet') {
      R(3, 2, 14, 3, p.hair);
      R(3, 3, 2, 3, p.hair); R(15, 3, 2, 3, p.hair);
      R(3, 2, 14, 1, p.hair2);
    }
    if (p.ahoge) { R(10, 0, 2, 2, p.hair); R(11, 0, 1, 1, p.hair2); }
    if (p.prop === 'helmet') {
      R(2, 1, 16, 2, p.accent); R(1, 3, 18, 2, p.accent);
      R(4, 0, 12, 1, '#fde68a'); R(2, 2, 16, 1, '#f59e0b');
    }
    if (p.glasses) {
      R(5, 6, 5, 1, OUT); R(5, 9, 5, 1, OUT); R(5, 7, 1, 2, OUT); R(9, 7, 1, 2, OUT);
      R(12, 6, 5, 1, OUT); R(12, 9, 5, 1, OUT); R(12, 7, 1, 2, OUT); R(16, 7, 1, 2, OUT);
      R(10, 7, 2, 1, OUT);
    }

    // face frames
    if (frame === 'blink' || frame === 'happy') {
      R(6, 8, 2, 1, OUT); R(12, 8, 2, 1, OUT);            // closed / smiling eyes
    } else if (frame === 'surprised') {
      R(6, 7, 2, 2, OUT); R(12, 7, 2, 2, OUT);
      R(6, 7, 1, 1, WHITE); R(12, 7, 1, 1, WHITE);
      R(9, 10, 2, 2, OUT); R(10, 10, 1, 1, '#7c2d12');     // open mouth
    } else if (frame === 'think') {
      R(6, 7, 2, 2, OUT); R(12, 8, 2, 1, OUT);             // one squint
      R(6, 7, 1, 1, WHITE);
      R(9, 10, 2, 1, MOUTH);
    } else {
      R(6, 7, 2, 2, OUT); R(12, 7, 2, 2, OUT);
      R(6, 7, 1, 1, WHITE); R(12, 7, 1, 1, WHITE);
      R(9, 10, 2, 1, MOUTH);
    }
    if (frame === 'talk') { R(9, 10, 2, 2, OUT); R(10, 11, 1, 1, '#7c2d12'); }
    if (frame === 'happy') { R(5, 9, 1, 1, BLUSH); R(14, 9, 1, 1, BLUSH); }
    else { R(5, 9, 1, 1, BLUSH); R(14, 9, 1, 1, BLUSH); }

    // props
    if (p.prop === 'headset') {
      R(3, 2, 14, 1, p.accent);
      R(2, 8, 2, 2, p.accent); R(16, 8, 2, 2, p.accent);
      R(15, 10, 1, 2, p.accent);
    }
    if (p.prop === 'tablet') {
      R(14, 16, 4, 5, OUT); R(15, 17, 3, 3, p.accent);
      R(15, 18, 2, 1, '#b45309');
    }
    if (p.prop === 'magnifier') {
      R(14, 16, 5, 1, '#a16207'); R(14, 20, 5, 1, '#a16207');
      R(14, 17, 1, 3, '#a16207'); R(18, 17, 1, 3, '#a16207');
      R(15, 17, 3, 2, '#bae6fd');
      R(16, 21, 1, 2, '#a16207'); R(16, 23, 1, 1, '#713f12');
    }
    if (p.prop === 'helmet') {
      R(15, 17, 2, 3, '#cbd5e1'); R(16, 20, 1, 2, '#94a3b8');
    }
    if (p.prop === 'check') {
      R(14, 16, 4, 5, OUT); R(15, 17, 3, 3, '#f8fafc');
      R(15, 18, 1, 1, p.accent); R(16, 19, 1, 1, p.accent); R(17, 18, 1, 1, p.accent);
    }
    if (p.prop === 'notebook') {
      R(14, 16, 4, 5, OUT); R(15, 17, 3, 3, '#f8fafc');
      R(15, 18, 2, 1, p.accent); R(15, 20, 2, 1, p.accent);
    }
    if (p.prop === 'megaphone') {
      R(13, 17, 3, 4, OUT); R(14, 18, 2, 2, p.accent);
      R(16, 16, 1, 6, OUT); R(17, 15, 1, 8, OUT);
      R(17, 16, 1, 6, p.accent);
    }
    if (p.prop === 'box') {
      R(14, 17, 4, 4, OUT); R(15, 18, 3, 3, '#b45309');
      R(15, 18, 3, 1, '#92400e');
    }
    if (p.grey) {
      R(9, 15, 2, 2, '#94a3b8');
      parts.push(`<text x="10" y="10" font-size="6" text-anchor="middle" fill="#e2e8f0" font-family="monospace" font-weight="bold">?</text>`);
    }

    const h = Math.round((width * 26) / 20);
    return `<svg viewBox="0 0 20 26" width="${width}" height="${h}" shape-rendering="crispEdges" style="image-rendering:pixelated;display:block">${parts.join('')}</svg>`;
  }

  window.PixelSprites = { svg, palette: PAL };
})();
