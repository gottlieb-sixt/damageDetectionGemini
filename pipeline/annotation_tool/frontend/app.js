// Damage Annotation Tool — Gemini Pro vs. Flash Vergleich
function app() {
  return {
    view: 'cars',
    cars: [],
    selectedCar: null,
    selectedImage: null,
    // Pro Modell separate State
    predictions: { gemini: null, flash: null },
    predMeta: { gemini: null, flash: null },
    predLatency: { gemini: null, flash: null },
    predMode: { gemini: null, flash: null },
    predCachedAt: { gemini: null, flash: null },
    showPreds: { gemini: true, flash: true },
    loadingPred: { gemini: false, flash: false },
    filterHasDamages: true,
    onlyTest: false,
    tileMode: false,
    stats: null,

    // Konva
    stage: null, layer: null, imgLayer: null,
    konvaImage: null,
    imageW: 0, imageH: 0,
    initialScale: 1,
    currentScale: 1,

    MODEL_COLOR: { gemini: '#ef4444', flash: '#3b82f6' },  // Pro=rot, Flash=blau
    MODEL_LABEL: { gemini: 'Gemini 3.1 Pro', flash: 'Gemini 3.5 Flash' },

    classes: ['scratch','stone_chip','dent','crack','missing','major','other'],
    classColors: {
      scratch: '#ff5050', stone_chip: '#ffa000', dent: '#3cb4ff',
      crack: '#c83cff', missing: '#ff3cc8', major: '#ff143c', other: '#787878',
    },
    classColor(c) { return this.classColors[c] || '#787878'; },

    // Lightbox
    lightboxPhoto: null, lightboxList: [], lightboxIdx: 0,

    async init() {
      await this.loadCars();
      await this.loadStats();
      window.addEventListener('keydown', e => this.handleKey(e));
    },

    async loadStats() {
      const r = await fetch('/api/stats');
      this.stats = await r.json();
    },

    async loadCars() {
      const r = await fetch(`/api/cars?has_damages=${this.filterHasDamages}&only_test=${this.onlyTest}&limit=600`);
      this.cars = await r.json();
    },

    async loadCar(plate) {
      const r = await fetch(`/api/cars/${plate}`);
      this.selectedCar = await r.json();
      this.view = 'car';
    },

    async openImage(imageId) {
      const r = await fetch(`/api/images/${imageId}`);
      this.selectedImage = await r.json();
      this.view = 'annotate';
      this.predictions = { gemini: null, flash: null };
      this.predMeta = { gemini: null, flash: null };
      this.predLatency = { gemini: null, flash: null };
      this.predMode = { gemini: null, flash: null };
      this.predCachedAt = { gemini: null, flash: null };
      await this.loadCachedPredictions();
      this.$nextTick(() => this.initKonva());
    },

    async loadCachedPredictions() {
      try {
        const r = await fetch(`/api/images/${this.selectedImage.id}/predictions_cached`);
        const cached = await r.json();
        for (const [modelKey, modelId] of [['gemini', 'gemini-3.1-pro'], ['flash', 'gemini-3.5-flash']]) {
          // Suche neuesten Eintrag für dieses Modell
          const matching = Object.entries(cached).filter(([k, _]) => k.includes(modelId));
          if (matching.length === 0) continue;
          matching.sort((a, b) => (b[1].created_at || 0) - (a[1].created_at || 0));
          const [key, entry] = matching[0];
          const parsed = entry.parsed;
          this.predictions[modelKey] = parsed.damages || parsed.visible_damages || [];
          this.predMeta[modelKey] = {
            n_calls: parsed.n_calls,
            n_pre_nms: parsed.n_pre_nms,
            from_tiles: parsed.from_tiles,
            n_reflection_clusters: parsed.n_reflection_clusters,
          };
          this.predLatency[modelKey] = entry.latency_s ? entry.latency_s.toFixed(1) : null;
          this.predMode[modelKey] = key.includes('#tiled') ? 'multi-scale-10x' : 'standard';
          this.predCachedAt[modelKey] = entry.created_at ? new Date(entry.created_at * 1000) : null;
        }
      } catch (e) {
        console.warn('Cache-Load Fehler:', e);
      }
    },

    initKonva() {
      const cont = document.getElementById('konva-container');
      cont.innerHTML = '';
      const imageUrl = `/api/images/${this.selectedImage.id}/file`;
      const img = new Image();
      img.onload = () => {
        const stageW = window.innerWidth - 360;
        const stageH = window.innerHeight - 80;
        const padding = 40;
        this.imageW = img.width;
        this.imageH = img.height;
        this.initialScale = Math.min(
          (stageW - padding * 2) / img.width,
          (stageH - padding * 2) / img.height,
          1
        );
        this.currentScale = this.initialScale;
        const offsetX = (stageW - img.width * this.initialScale) / 2;
        const offsetY = (stageH - img.height * this.initialScale) / 2;
        this.stage = new Konva.Stage({
          container: 'konva-container',
          width: stageW, height: stageH,
          draggable: true,
          x: offsetX, y: offsetY,
          scaleX: this.initialScale, scaleY: this.initialScale,
        });
        this.imgLayer = new Konva.Layer();
        this.layer = new Konva.Layer();
        this.stage.add(this.imgLayer);
        this.stage.add(this.layer);
        this.konvaImage = new Konva.Image({ image: img, width: img.width, height: img.height });
        this.imgLayer.add(this.konvaImage);
        this.stage.on('wheel', (e) => {
          e.evt.preventDefault();
          const scaleBy = 1.15;
          const oldScale = this.stage.scaleX();
          const pointer = this.stage.getPointerPosition();
          const mousePointTo = {
            x: (pointer.x - this.stage.x()) / oldScale,
            y: (pointer.y - this.stage.y()) / oldScale,
          };
          const direction = e.evt.deltaY < 0 ? 1 : -1;
          let newScale = direction > 0 ? oldScale * scaleBy : oldScale / scaleBy;
          newScale = Math.max(this.initialScale * 0.5, Math.min(newScale, 10));
          this.stage.scale({ x: newScale, y: newScale });
          this.stage.position({
            x: pointer.x - mousePointTo.x * newScale,
            y: pointer.y - mousePointTo.y * newScale,
          });
          this.currentScale = newScale;
        });
        this.redraw();
      };
      img.src = imageUrl;
    },

    resetZoom() {
      if (!this.stage) return;
      const stageW = this.stage.width(), stageH = this.stage.height();
      const offsetX = (stageW - this.imageW * this.initialScale) / 2;
      const offsetY = (stageH - this.imageH * this.initialScale) / 2;
      this.stage.scale({ x: this.initialScale, y: this.initialScale });
      this.stage.position({ x: offsetX, y: offsetY });
      this.currentScale = this.initialScale;
    },

    zoomBy(factor) {
      if (!this.stage) return;
      const oldScale = this.stage.scaleX();
      const stageW = this.stage.width(), stageH = this.stage.height();
      const centerX = stageW / 2, centerY = stageH / 2;
      const focusInImage = {
        x: (centerX - this.stage.x()) / oldScale,
        y: (centerY - this.stage.y()) / oldScale,
      };
      let newScale = Math.max(this.initialScale * 0.5, Math.min(oldScale * factor, 10));
      this.stage.scale({ x: newScale, y: newScale });
      this.stage.position({
        x: centerX - focusInImage.x * newScale,
        y: centerY - focusInImage.y * newScale,
      });
      this.currentScale = newScale;
    },

    redraw() {
      if (!this.layer) return;
      this.layer.destroyChildren();
      ['gemini', 'flash'].forEach(model => {
        if (!this.showPreds[model] || !this.predictions[model]) return;
        const color = this.MODEL_COLOR[model];
        this.predictions[model].forEach((d, idx) => {
          const [ymin, xmin, ymax, xmax] = d.bbox_2d;
          const x = xmin/1000 * this.imageW;
          const y = ymin/1000 * this.imageH;
          const w = (xmax-xmin)/1000 * this.imageW;
          const h = (ymax-ymin)/1000 * this.imageH;
          const isCluster = d._is_cluster;
          const strokeColor = isCluster ? '#fbbf24' : color;
          const rect = new Konva.Rect({
            x, y, width: w, height: h,
            stroke: strokeColor, strokeWidth: 3,
            dash: isCluster ? [10, 5] : undefined,
            fill: isCluster ? strokeColor + '20' : undefined,
          });
          this.layer.add(rect);
          // Pro: Label oben · Flash: Label unten (damit sie sich nicht überdecken)
          const labelY = model === 'gemini' ? y - 22 : y + h + 2;
          const tagText = isCluster
            ? `⚠️ ${this.MODEL_LABEL[model]}: Cluster (${d._cluster_size}×)`
            : `${this.MODEL_LABEL[model]}: ${d.label} ${Math.round((d.confidence||0)*100)}%`;
          const lbl = new Konva.Label({ x, y: Math.max(0, labelY) });
          lbl.add(new Konva.Tag({ fill: strokeColor }));
          lbl.add(new Konva.Text({
            text: tagText, fontSize: 12, padding: 4, fill: 'white', fontStyle: 'bold',
          }));
          this.layer.add(lbl);
        });
      });
      this.layer.batchDraw();
    },

    async runModel(model) {
      this.loadingPred[model] = true;
      const t0 = performance.now();
      const mode = this.tileMode ? 'multi-scale-10x' : 'standard';
      try {
        const tiledParam = this.tileMode ? '&tiled=true' : '';
        const r = await fetch(`/api/images/${this.selectedImage.id}/predictions?model=${model}${tiledParam}&force=true`);
        const data = await r.json();
        this.predLatency[model] = ((performance.now() - t0) / 1000).toFixed(1);
        this.predMode[model] = mode;
        this.predCachedAt[model] = new Date();
        const parsed = Object.values(data)[0];
        if (parsed.error) { alert(`${this.MODEL_LABEL[model]} Fehler: ${parsed.error}`); return; }
        this.predictions[model] = parsed.damages || parsed.visible_damages || [];
        this.predMeta[model] = {
          n_calls: parsed.n_calls,
          n_pre_nms: parsed.n_pre_nms,
          from_tiles: parsed.from_tiles,
          n_reflection_clusters: parsed.n_reflection_clusters,
        };
        this.redraw();
      } catch (e) {
        alert('Fehler: ' + e);
      } finally {
        this.loadingPred[model] = false;
      }
    },

    async runBoth() {
      await Promise.all([this.runModel('gemini'), this.runModel('flash')]);
    },

    formatTimeAgo(date) {
      if (!date) return '';
      const sec = (new Date() - date) / 1000;
      if (sec < 60) return `vor ${Math.round(sec)}s`;
      if (sec < 3600) return `vor ${Math.round(sec/60)} min`;
      if (sec < 86400) return `vor ${Math.round(sec/3600)} h`;
      return `vor ${Math.round(sec/86400)} d`;
    },

    async nextImage() {
      const imgs = this.selectedCar.images;
      const idx = imgs.findIndex(i => i.id === this.selectedImage.id);
      if (idx < imgs.length - 1) await this.openImage(imgs[idx+1].id);
    },
    async prevImage() {
      const imgs = this.selectedCar.images;
      const idx = imgs.findIndex(i => i.id === this.selectedImage.id);
      if (idx > 0) await this.openImage(imgs[idx-1].id);
    },

    closeAnnotation() {
      this.view = 'car';
      this.selectedImage = null;
    },

    // === Damage Cases ===
    get allDamagePhotosFlat() {
      return (this.selectedCar?.damage_cases || []).flatMap(c => c.photos);
    },

    damageSummary(damagesList) {
      if (!damagesList || damagesList.length === 0) return 'keine Damage-Info';
      const counts = {};
      for (const d of damagesList) counts[d.master_class] = (counts[d.master_class] || 0) + 1;
      return Object.entries(counts)
        .sort((a, b) => b[1] - a[1])
        .map(([cls, n]) => `${n}× ${cls}`)
        .join(' · ');
    },

    openLightbox(photo) {
      this.lightboxList = this.allDamagePhotosFlat;
      this.lightboxIdx = this.lightboxList.findIndex(p => p.url === photo.url);
      this.lightboxPhoto = photo;
    },
    closeLightbox() { this.lightboxPhoto = null; },
    nextLightbox() {
      if (this.lightboxIdx < this.lightboxList.length - 1) {
        this.lightboxIdx++;
        this.lightboxPhoto = this.lightboxList[this.lightboxIdx];
      }
    },
    prevLightbox() {
      if (this.lightboxIdx > 0) {
        this.lightboxIdx--;
        this.lightboxPhoto = this.lightboxList[this.lightboxIdx];
      }
    },

    handleKey(e) {
      if (this.lightboxPhoto) {
        const k = e.key.toLowerCase();
        if (k === 'escape') { this.closeLightbox(); e.preventDefault(); }
        else if (k === 'arrowleft') { this.prevLightbox(); e.preventDefault(); }
        else if (k === 'arrowright') { this.nextLightbox(); e.preventDefault(); }
        return;
      }
      if (this.view !== 'annotate') return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
      const k = e.key.toLowerCase();
      if (k === 'g') { this.runModel('gemini'); e.preventDefault(); }
      else if (k === 'f') { this.runModel('flash'); e.preventDefault(); }
      else if (k === 'b') { this.runBoth(); e.preventDefault(); }
      else if (k === 'arrowleft') { this.prevImage(); e.preventDefault(); }
      else if (k === 'arrowright') { this.nextImage(); e.preventDefault(); }
      else if (k === 'escape') { this.closeAnnotation(); e.preventDefault(); }
      else if (k === 't') { this.tileMode = !this.tileMode; e.preventDefault(); }
    },
  };
}
