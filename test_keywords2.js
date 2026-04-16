// Test word boundary matching - focused on the actual problem
function escapeRegExp(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function _normalize(text) {
  if (!text) return "";
  return text.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

function _wordRegex(word) {
  if (!word) return null;
  var normalizedWord = _normalize(word);
  if (!normalizedWord) return null;
  // Szuka całego wyrazu, nie podciągu
  // Używa (^|[^\w]) zamiast \b, aby uniknąć dopasowania części słowa
  // np. "ania" NIE pasuje do "śpiewania"
  var escaped = escapeRegExp(normalizedWord);
  // (?<!\w) i (?!\w) są lookbehind/lookahead, ale GAS może nie obsługiwać
  // Używamy (^|[^\w]) dla początku i ([^\w]|$) dla końca
  return new RegExp('(^|[^\\w])' + escaped + '([^\\w]|$)', "i");
}

function _containsAny(haystack, keywords) {
  if (!haystack || !keywords || !keywords.length) return false;
  var normalizedHaystack = _normalize(haystack);
  return keywords.some(function(k) {
    if (!k) return false;
    var re = _wordRegex(k);
    return re ? re.test(normalizedHaystack) : false;
  });
}

// Test cases for the exact problem
console.log('=== Testing the exact problem: "Ania" inside words ===');

// Test A: "ania" inside "śpiewania" (no spaces) - should NOT match
const testAText = 'śpiewania';
const testAKeywords = ['Ania'];
console.log('Test A - Text:', testAText, 'Keywords:', testAKeywords);
console.log('Normalized text:', _normalize(testAText));
console.log('Normalized keyword:', _normalize(testAKeywords[0]));
const re = _wordRegex(testAKeywords[0]);
console.log('Regex pattern:', re ? re.source : 'null');
console.log('Regex test result:', re ? re.test(_normalize(testAText)) : false);
console.log('_containsAny result:', _containsAny(testAText, testAKeywords));

// Test B: "ania" inside "kania" (no spaces) - should NOT match
const testBText = 'kania';
const testBKeywords = ['Ania'];
console.log('\nTest B - Text:', testBText, 'Keywords:', testBKeywords);
console.log('Normalized text:', _normalize(testBText));
console.log('_containsAny result:', _containsAny(testBText, testBKeywords));

// Test C: "ania" as separate word "Ania" - should match
const testCText = 'Ania';
const testCKeywords = ['Ania'];
console.log('\nTest C - Text:', testCText, 'Keywords:', testCKeywords);
console.log('Normalized text:', _normalize(testCText));
console.log('_containsAny result:', _containsAny(testCText, testCKeywords));

// Test D: "ania" in phrase "Ania pisze" - should match only "Ania"
const testDText = 'Ania pisze';
const testDKeywords = ['Ania'];
console.log('\nTest D - Text:', testDText, 'Keywords:', testDKeywords);
console.log('_containsAny result:', _containsAny(testDText, testDKeywords));

// Test E: "scrabble" inside "scrabbleboard" - should NOT match
const testEText = 'scrabbleboard';
const testEKeywords = ['scrabble'];
console.log('\nTest E - Text:', testEText, 'Keywords:', testEKeywords);
console.log('_containsAny result:', _containsAny(testEText, testEKeywords));

// Test F: "generator pdf" inside "generator pdfx" - should NOT match
const testFText = 'generator pdfx';
const testFKeywords = ['generator pdf'];
console.log('\nTest F - Text:', testFText, 'Keywords:', testFKeywords);
console.log('_containsAny result:', _containsAny(testFText, testFKeywords));

// Test G: Check that normalization removes diacritics
const testGText = 'śpiewania';
const testGKeywords = ['spiewania']; // normalized form
console.log('\nTest G - Normalization test');
console.log('Text:', testGText, 'Normalized:', _normalize(testGText));
console.log('Keyword:', testGKeywords[0], 'Normalized:', _normalize(testGKeywords[0]));
console.log('_containsAny result:', _containsAny(testGText, testGKeywords));

// Test H: Multi-word keyword "generator pdf" with boundaries
const testHText = 'to jest generator pdf test';
const testHKeywords = ['generator pdf'];
console.log('\nTest H - Text:', testHText, 'Keywords:', testHKeywords);
console.log('_containsAny result:', _containsAny(testHText, testHKeywords));

// Test I: Keyword with punctuation "Ania!" 
const testIText = 'Ania!';
const testIKeywords = ['Ania'];
console.log('\nTest I - Text:', testIText, 'Keywords:', testIKeywords);
console.log('_containsAny result:', _containsAny(testIText, testIKeywords));

console.log('\n=== Summary ===');
console.log('The fix should prevent "Ania" from matching inside "śpiewania".');
console.log('If Test A returns false, the fix works.');