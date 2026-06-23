async function loadHealth(){
    const r = await fetch('/api/health');
    const data = await r.json();

    document.getElementById("health").innerText =
        data.status.toUpperCase();
}

loadHealth();