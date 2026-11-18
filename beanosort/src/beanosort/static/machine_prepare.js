(() => {
 const recipe=document.querySelector('[name="recipe_id"]'), group=document.querySelector('#machine-prepare');
 if(!recipe || !group)return;
 const update=()=>{const active=recipe.selectedOptions[0]?.dataset.machineRecipe==='true';group.hidden=!active;document.querySelectorAll('[data-synthetic-settings]').forEach(el=>el.hidden=active);group.disabled=!active; if(active){const station=document.querySelector('[name="station_id"]');const option=station?.querySelector('[data-machine-station="true"]');if(option)station.value=option.value;}};
 recipe.addEventListener('change',update);update();
})();
