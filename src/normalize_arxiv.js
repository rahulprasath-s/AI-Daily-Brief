const entries = items[0]?.json?.feed?.entry || [];

return entries.map(paper => ({
  json: {
    title: paper.title,
    summary: paper.summary,
    url: paper.id,
    source: "ArXiv Research",
    published_at: paper.published,
    type: "Research"
  }
}));