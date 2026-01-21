return items.map(item => ({
  json: {
    title: item.json.title,
    summary: item.json.contentSnippet || item.json.content || "",
    url: item.json.link,
    source: "Tech Blog",
    published_at: item.json.pubDate,
    type: "News"
  }
}));