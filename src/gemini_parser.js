// 1. Safe access: Use '?.' and provide a default empty string.
// We also check commonly used keys just in case the name changed.
const response = items[0]?.json?.content || items[0]?.json?.text || items[0]?.json?.output || '';

// 2. Safety check: Ensure 'response' is a string before running string methods.
// If the AI returned an Object directly, we might not need to parse it.
if (typeof response !== 'string') {
  // If it's already an object, return it wrapped in the expected format
  if (typeof response === 'object' && response !== null) {
     return [ { json: response } ];
  }
  return []; // Return empty if data is unusable
}

// 3. Clean the markdown
const cleanJson = response.replace(/```json/g, '').replace(/```/g, '').trim();

try {
  const parsed = JSON.parse(cleanJson);
  // Ensure parsed.items exists and is an array before mapping
  if (parsed.items && Array.isArray(parsed.items)) {
      return parsed.items.map(item => ({ json: item }));
  } else {
      // If parsed JSON is valid but doesn't have an 'items' array
      return [ { json: parsed } ];
  }
} catch (error) {
  // Fallback if AI output valid text but invalid JSON
  return [];
}