// Detailed tables load after the operator opens them, outside the save/navigation path.
document.querySelectorAll('details[data-lazy-details]').forEach(details=>{
  if (details.dataset.lazyReady) return;
  details.dataset.lazyReady='true';
  let pending=false,loaded=false;
  details.addEventListener('toggle',async()=>{
    if (!details.open || pending || loaded) return;
    pending=true;
    const content=details.querySelector('[data-lazy-content]');
    content.setAttribute('aria-busy','true');
    const status=document.createElement('p');status.textContent='Loading population distributions…';status.setAttribute('role','status');content.replaceChildren(status);
    try {
      const response=await fetch(details.dataset.lazyDetails,{credentials:'same-origin',cache:'no-store',headers:{'X-BeanoSort-Fragment':'1'}});
      if (!response.ok || response.redirected || !response.headers.get('content-type')?.includes('text/html')) throw new Error('Unable to load distributions. Reload the page or sign in again.');
      content.innerHTML=await response.text();loaded=true;
    } catch (error) {
      status.textContent=error.message;
      const link=document.createElement('a');link.href=details.dataset.lazyDetails;link.textContent='Open population distributions';content.append(link);
    } finally {pending=false;content.removeAttribute('aria-busy');}
  });
});
