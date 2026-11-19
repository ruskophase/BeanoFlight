// Read-only histogram exploration; no stock writes or machine-control requests.
const populationData = document.querySelector('#population-data');
if (populationData) {
  const report = JSON.parse(populationData.textContent);
  const basis = document.querySelector('[data-population-basis]');
  const scale = document.querySelector('[data-population-scale]');
  const tooltip = document.querySelector('[data-chart-tooltip]');
  const explorer = document.querySelector('[data-population-explorer]');
  const focusFeature = explorer.dataset.focusFeature || '';
  const view = new URLSearchParams(window.location.search);
  const inspector = document.querySelector('[data-population-inspector]');
  const chooser = document.querySelector('[data-inspector-population]');
  const status = document.querySelector('[data-inspector-status]');
  const gallery = document.querySelector('[data-inspector-beans]');
  const retry = document.querySelector('[data-inspector-retry]');
  const previous = document.querySelector('[data-inspector-prev]');
  const next = document.querySelector('[data-inspector-next]');
  const toggles = [...document.querySelectorAll('[data-population-toggle]')];
  const series = report.series || (report.single_population ? [{key:'input',title:'Current contents',colour:'#64748b'}] :
    [{key:'input',title:'Input',colour:'#64748b'}, {key:'selected',title:'Selected',colour:'#206a55'}, {key:'pass_through',title:'Pass-through',colour:'#bc6414'}]);
  for (const item of series) if (report.series_titles?.[item.key]) item.title = report.series_titles[item.key];
  for (const option of [...chooser.options]) {
    const item = series.find(s => s.key === option.value);
    if (item) option.textContent = item.title; else option.remove();
  }
  const ns = 'http://www.w3.org/2000/svg';
  const number = value => Number(value).toLocaleString(undefined, {maximumFractionDigits: 3});
  const keyboardBins = new Map();
  let selection = null, controller = null, requestId = 0;
  function visible() { return series.filter(s => toggles.some(t => t.dataset.populationToggle === s.key && t.checked)); }
  function populationKey(key) { return key === 'input' ? key : basis.value+'_'+key; }
  function node(tag, attrs, text) {
    const el = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function html(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  function closeInspector() {
    controller?.abort(); requestId++; selection = null;
    inspector.hidden = !focusFeature; explorer.classList.toggle('inspecting',Boolean(focusFeature));
    if(focusFeature){
      gallery.replaceChildren();status.textContent='Click a chart interval to view matching beans and colour samples.';
      document.querySelector('[data-inspector-interval]').textContent='Choose an interval';
      document.querySelector('[data-inspector-evidence]').textContent='';
      document.querySelector('[data-inspector-page]').textContent='';
      inspector.setAttribute('aria-busy','false');retry.hidden=true;previous.disabled=next.disabled=true;
    }
    render();
  }
  function viewLink(link, feature) {
    const url=new URL(link.href,window.location.origin);
    for(const key of ['view_feature','view_bucket','view_population','view_page'])url.searchParams.delete(key);
    url.searchParams.set('view_basis',basis.value);url.searchParams.set('view_scale',scale.value);
    url.searchParams.set('view_lines',visible().map(s=>s.key).join(','));
    if(selection&&(!feature||selection.feature===feature)){
      url.searchParams.set('view_feature',selection.feature);url.searchParams.set('view_bucket',selection.bucket);
      url.searchParams.set('view_population',selection.population);url.searchParams.set('view_page',selection.page||1);
    }
    link.href=url.pathname+url.search+url.hash;
  }
  function renderBean(bean, data) {
    const card = html('article');
    card.dataset.beanSequence = bean.bean_sequence;
    const views = html('div', undefined, 'inspector-views');
    for (const camera of ['CamL','CamR']) {
      const view = html('div');
      if (bean.photos[camera]) {
        const link = html('a'); link.href = bean.photos[camera]; link.target = '_blank'; link.rel = 'noopener';
        const photo = html('img'); photo.src = bean.photos[camera]; photo.loading = 'lazy';
        photo.alt = `${data.measurement_evidence ? 'Saved crop' : 'Reference'} ${bean.bean_id} ${camera}`;
        photo.addEventListener('error',()=>link.replaceWith(html('small','Photograph unavailable — colour sample retained')),{once:true});
        link.append(photo); view.append(link);
      } else view.append(html('small','Photograph unavailable'));
      const label = html('small', camera+' mean colour');
      const rgb = bean.colours[camera];
      if (rgb) {
        const swatch = html('span', undefined, 'bean-colour-swatch');
        swatch.style.backgroundColor = `rgb(${rgb.join(',')})`;
        swatch.title = `${camera} recorded mean RGB: ${rgb.join(', ')}`;
        swatch.setAttribute('role','img'); swatch.setAttribute('aria-label',swatch.title);
        label.prepend(swatch);
      } else label.append(html('small','Colour unknown'));
      view.append(label); views.append(view);
    }
    card.append(views, html('strong',bean.bean_id), html('small',`${data.title}: ${number(bean.feature_value)} ${data.unit}`));
    if (bean.photo_frame_index !== undefined) card.append(html('small',`Saved crop frame ${bean.photo_frame_index}; features aggregate ${bean.sample_count ?? 'unknown'} sample(s)`));
    if (bean.origin_run_id) {
      const origin=html('a',bean.origin_run_id,'identity');origin.href=bean.origin_url;
      card.append(html('small',`${bean.origin_source==='live'?'Live observations':'Recorded replay'} · ${bean.origin_image_mode==='stereo'?'Stereo crop mode':bean.origin_image_mode==='none'?'No image saves':'RAW frames'}`),origin);
    }
    const expected = data.basis === 'expected' && data.population !== 'input';
    card.append(html('small', `${number(bean.count)} ${data.count_unit || (expected ? 'expected passages' : data.run_id ? 'simulated passages' : 'reference bean')}`));
    if (expected && data.run_id) card.append(html('small',`${number(bean.recorded_passages)} candidate passages; destination probabilistic`));
    if (bean.missed_target_passages) card.append(html('small',`${number(bean.missed_target_passages)} simulated missed target passage(s)`));
    return card;
  }
  async function loadInspection(page = 1) {
    if (!selection) return;
    selection.page = page;
    controller?.abort(); controller = new AbortController(); const currentRequest = ++requestId;
    inspector.hidden = false; inspector.setAttribute('aria-busy','true'); explorer.classList.add('inspecting');
    for (const option of chooser.options) option.disabled = !visible().some(s => s.key === option.value);
    chooser.value = selection.population;
    const spec = report.definitions[selection.feature];
    document.querySelector('[data-inspector-interval]').textContent =
      `${spec.title}: [${number(spec.edges[selection.bucket])}, ${number(spec.edges[selection.bucket+1])}) ${spec.unit}`;
    status.textContent = 'Loading corresponding beans…'; gallery.replaceChildren(); retry.hidden = true;
    document.querySelector('[data-inspector-evidence]').textContent = '';
    previous.disabled = true; next.disabled = true;
    document.querySelector('[data-inspector-page]').textContent = '';
    render();
    try {
      if (!explorer.dataset.inspectionUrl) throw new Error('Reload this page after the server update to inspect beans.');
      const url = new URL(explorer.dataset.inspectionUrl, window.location.origin);
      for (const [key,value] of Object.entries({...selection,basis:basis.value,page})) url.searchParams.set(key,value);
      const response = await fetch(url, {signal:controller.signal, cache:'no-store'});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json'))
        throw new Error('Could not load beans. Check your connection or sign in again, then retry.');
      const data = await response.json();
      if (currentRequest !== requestId) return;
      selection.page = data.page;
      const title = series.find(s => s.key === data.population).title;
      status.textContent = data.measurement_evidence ? `${title}: ${number(data.matching_count)} matching tracked records.` :
        `${title}: ${number(data.matching_count)} ${data.count_basis} · ${number(data.reference_bean_count)} reference bean(s).`;
      if (data.measurement_evidence && data.photographed_count !== undefined) status.textContent+=
        ` ${number(data.photographed_count)} with saved crop records; ${number(data.missing_photo_reference_count)} without. ${data.photo_ordering}.`;
      document.querySelector('[data-inspector-evidence]').textContent =
        data.measurement_evidence ? data.evidence_label : (data.retained ? 'Retained joint population estimate; physical membership and mixing uniformity are unverified. ' : data.collection ? 'Count-weighted combined composition estimate; actual uniformity and physical membership are unverified. ' : data.basis === 'expected' && data.population !== 'input' ?
          'Expected contributors weighted by capture probability; these are possible destinations, not verified arrivals. ' :
          data.run_id ? 'Stored simulated passage records; no physical capture confirmation. ' : 'Reference preview; each archived bean is used once. ') +
        'Photos and mean RGB colour samples are archived source evidence. Repeated passages share one photo pair.';
      gallery.replaceChildren(...data.items.map(bean => renderBean(bean,data)));
      if (!data.items.length) gallery.append(html('p','No corresponding beans in this interval for this population.'));
      previous.disabled = data.page <= 1; next.disabled = data.page >= data.pages;
      document.querySelector('[data-inspector-page]').textContent = `Page ${data.page} of ${data.pages}`;
      inspector.setAttribute('aria-busy','false');
    } catch (error) {
      if (error.name === 'AbortError' || currentRequest !== requestId) return;
      status.textContent = error.message; retry.hidden = false;
      inspector.setAttribute('aria-busy','false');
    }
  }
  function inspect(feature, bucket, population) {
    selection = {feature,bucket,population};
    loadInspection();
    if (window.matchMedia(`(max-width: ${focusFeature?1050:950}px)`).matches) inspector.scrollIntoView({block:'start'});
  }
  function describeBin(feature, bucket) {
    const spec = report.definitions[feature];
    tooltip.textContent = `${spec.title} [${number(spec.edges[bucket])}, ${number(spec.edges[bucket+1])}) ${spec.unit}: ` +
      visible().map(s => `${s.title} ${number(report.populations[populationKey(s.key)].histograms[feature][bucket])}`).join(' · ') +
      ` (${report.measurement_evidence ? 'recorded image-derived features; intended output membership' : (report.combination || report.single_population) ? 'retained composition estimate' : basis.value === 'expected' ? 'expected outputs' : 'synthetic destination outcomes'}).`;
  }
  function render() {
    const shown = visible();
    for (const option of chooser.options) option.disabled = !shown.some(s => s.key === option.value);
    if (selection) chooser.value = selection.population;
    for (const svg of document.querySelectorAll('[data-population-chart]')) {
      const feature = svg.dataset.populationChart, spec = report.definitions[feature], edges = spec.edges;
      const width=focusFeature?Math.max(320,Math.round(svg.clientWidth)):600;
      const height=focusFeature?Math.max(360,Math.min(650,Math.round(width*.58))):300;
      const left=focusFeature?64:54,right=focusFeature?width-24:580,top=focusFeature?30:22,bottom=focusFeature?height-50:256;
      if(focusFeature)svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
      const x = value => left+(value-edges[0])/(edges[edges.length-1]-edges[0])*(right-left);
      const values = shown.map(s => {
        const pop = report.populations[populationKey(s.key)];
        return pop.histograms[feature].map(value => scale.value === 'percent' ?
          (Number(pop.count) ? Number(value)/Number(pop.count)*100 : null) : Number(value));
      });
      const max = Math.max(1,...values.flat().filter(v => v !== null))*1.08;
      const y = value => bottom-value/max*(bottom-top);
      svg.replaceChildren();
      svg.append(node('title',{},spec.title+': '+shown.map(s => s.title).join(', ')));
      for (let i=0;i<=4;i++) {
        const value=max*i/4;
        svg.append(node('line',{x1:left,x2:right,y1:y(value),y2:y(value),stroke:'#dce2d8'}));
        svg.append(node('text',{x:left-7,y:y(value)+4,'text-anchor':'end','font-size':11,fill:'#61716b'},number(value)+(scale.value==='percent'?'%':'')));
        const valueX=edges[0]+(edges[edges.length-1]-edges[0])*i/4;
        svg.append(node('text',{x:x(valueX),y:bottom+22,'text-anchor':'middle','font-size':11,fill:'#61716b'},number(valueX)));
      }
      if (selection?.feature === feature) svg.append(node('rect',{
        x:x(edges[selection.bucket]),y:top,width:x(edges[selection.bucket+1])-x(edges[selection.bucket]),height:bottom-top,
        fill:'#e8cc7c','fill-opacity':.35,'data-inspected-bin':selection.bucket,'pointer-events':'none'}));
      values.forEach((points,index) => {
        if (points.every(value => value === null)) return;
        let path='';
        points.forEach((value,i) => {path+=`${i?'L':'M'}${x(edges[i])},${y(value||0)} L${x(edges[i+1])},${y(value||0)} `;});
        svg.append(node('path',{d:path,fill:'none',stroke:shown[index].colour,'stroke-width':2.5,'data-population-series':shown[index].key}));
      });
      if (!shown.length) svg.append(node('text',{x:width/2,y:height/2,'text-anchor':'middle','font-size':15,fill:'#61716b'},'Select a population line above.'));
      if (feature === report.recipe.feature && report.recipe.relation !== 'pass_all') {
        const bounds=report.recipe.relation==='below'?['lower']:report.recipe.relation==='above'?['upper']:['lower','upper'];
        for (const bound of bounds) {
          const value=Number(report.recipe[bound]); if (value<edges[0]||value>edges[edges.length-1]) continue;
          const line=node('line',{x1:x(value),x2:x(value),y1:top,y2:bottom,stroke:'#263a35','stroke-dasharray':'4 4'});
          line.append(node('title',{},'Recipe threshold: '+number(value)+' '+spec.unit));svg.append(line);
        }
      }
      document.querySelector(`[data-chart-summary="${feature}"]`).textContent=shown.map(s => {
        const pop=report.populations[populationKey(s.key)];return `${s.title}: ${number(pop.count)} ${report.measurement_evidence ? 'tracked records' : (report.combination || report.single_population) ? 'estimated beans' : 'passages'}, ${number(pop.unknown[feature])} unknown`;
      }).join(' · ');
      function position(event) {
        const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY;
        return point.matrixTransform(svg.getScreenCTM().inverse());
      }
      function binAt(point) {
        if (point.x<left||point.x>right||point.y<top||point.y>bottom) return -1;
        const value=edges[0]+(point.x-left)/(right-left)*(edges[edges.length-1]-edges[0]);
        return point.x===right?edges.length-2:edges.findIndex((lo,i)=>i<edges.length-1&&value>=lo&&value<edges[i+1]);
      }
      svg.onmousemove=event=>{const bucket=binAt(position(event));if (bucket>=0) describeBin(feature,bucket);};
      svg.onclick=event=>{
        const point=position(event), bucket=binAt(point);if (bucket<0||!shown.length) return;
        const preferred=explorer.dataset.defaultPopulation;
        const ranked=shown.map((s,index)=>({key:s.key,distance:values[index][bucket]===null?Infinity:Math.abs(point.y-y(values[index][bucket]))}));
        ranked.sort((a,b)=>a.distance-b.distance||(a.key===preferred?-1:b.key===preferred?1:0));
        inspect(feature,bucket,ranked[0].key);
      };
      svg.onkeydown=event=>{
        let bucket=keyboardBins.get(feature)??0;
        if (event.key==='ArrowLeft'||event.key==='ArrowRight') {
          event.preventDefault();bucket=Math.max(0,Math.min(edges.length-2,bucket+(event.key==='ArrowRight'?1:-1)));
          keyboardBins.set(feature,bucket);describeBin(feature,bucket);
        } else if ((event.key==='Enter'||event.key===' ')&&shown.length) {
          event.preventDefault();inspect(feature,bucket,shown.find(s=>s.key===explorer.dataset.defaultPopulation)?.key||shown[0].key);
        } else if (event.key==='Escape') closeInspector();
      };
    }
    for(const link of document.querySelectorAll('[data-chart-link]'))viewLink(link,link.dataset.chartLink);
    const back=document.querySelector('[data-chart-back]');if(back)viewLink(back);
  }
  toggles.forEach(toggle=>toggle.addEventListener('change',()=>{
    const shown=visible();
    if (!shown.length) {closeInspector();return;}
    if (selection&&!shown.some(s=>s.key===selection.population)) {selection.population=shown[0].key;loadInspection();}
    else render();
  }));
  chooser.addEventListener('change',()=>{if(selection){selection.population=chooser.value;loadInspection();}});
  document.querySelector('[data-inspector-close]').addEventListener('click',closeInspector);
  previous.addEventListener('click',()=>loadInspection(selection.page-1));
  next.addEventListener('click',()=>loadInspection(selection.page+1));
  retry.addEventListener('click',()=>loadInspection(selection?.page||1));
  basis.addEventListener('change',()=>{render();if(selection)loadInspection();});
  scale.addEventListener('change',render);
  if([...basis.options].some(o=>o.value===view.get('view_basis')))basis.value=view.get('view_basis');
  if([...scale.options].some(o=>o.value===view.get('view_scale')))scale.value=view.get('view_scale');
  if(view.has('view_lines')){
    const chosen=new Set(view.get('view_lines').split(','));
    for(const toggle of toggles)toggle.checked=chosen.has(toggle.dataset.populationToggle);
  }
  if(focusFeature){
    document.querySelector('main').classList.add('population-focus-page');explorer.classList.add('population-focus');
    explorer.append(inspector); // Keep reading/tab order aligned with chart-first layout.
    const clear=document.querySelector('[data-inspector-close]');clear.textContent='Clear selection';clear.setAttribute('aria-label','Clear bean inspection');
    closeInspector();
    let lastWidth=0;
    new ResizeObserver(entries=>{const width=Math.round(entries[0].contentRect.width);if(width!==lastWidth){lastWidth=width;render();}}).observe(document.querySelector('.population-charts'));
  }
  const initialFeature=focusFeature||view.get('view_feature'),bucket=Number(view.get('view_bucket'));
  if(view.has('view_bucket')&&report.definitions[initialFeature]&&Number.isInteger(bucket)&&bucket>=0&&bucket<report.definitions[initialFeature].edges.length-1&&visible().length){
    selection={feature:initialFeature,bucket,population:visible().find(s=>s.key===view.get('view_population'))?.key||visible()[0].key};
    keyboardBins.set(initialFeature,bucket);
    const page=Number(view.get('view_page'));loadInspection(Number.isInteger(page)&&page>0?page:1);
  }else render();
}
