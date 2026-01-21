const uniqueItems = new Map();
items.forEach(item => {
  const hash = item.json.title.toLowerCase().replace(/[^a-z0-9]/g, '');
  if (!uniqueItems.has(hash)) {
    uniqueItems.set(hash, item);
  }
});
return Array.from(uniqueItems.values());