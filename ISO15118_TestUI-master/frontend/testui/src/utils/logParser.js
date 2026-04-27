/**
 * ISO 15118 Protocol Log Parser
 * Parses test execution logs to extract message exchanges for sequence diagram rendering
 */

// ISO 15118 message types mapping
const MESSAGE_TYPES = {
  // V2GTP Messages
  'V2GTP_TCP_TLS_LAYER_REQUEST': 'V2GTP TCP/TLS Request',
  'V2GTP_TCP_TLS_RESPONSE': 'V2GTP TCP/TLS Response',
  'SDP': 'Service Discovery Protocol',
  'V2G_SDP': 'V2G Service Discovery',

  // Session Management
  'SessionSetupReq': 'SessionSetup Request',
  'SessionSetupRes': 'SessionSetup Response',
  'SessionStopReq': 'SessionStop Request',
  'SessionStopRes': 'SessionStop Response',

  // Service Discovery
  'ServiceDiscoveryReq': 'ServiceDiscovery Request',
  'ServiceDiscoveryRes': 'ServiceDiscovery Response',
  'ServiceDetailReq': 'ServiceDetail Request',
  'ServiceDetailRes': 'ServiceDetail Response',

  // Payment
  'PaymentServiceSelectionReq': 'PaymentServiceSelection Request',
  'PaymentServiceSelectionRes': 'PaymentServiceSelection Response',
  'PaymentDetailsReq': 'PaymentDetails Request',
  'PaymentDetailsRes': 'PaymentDetails Response',

  // Authorization
  'AuthorizationReq': 'Authorization Request',
  'AuthorizationRes': 'Authorization Response',
  'ChargeProgress': 'Charge Progress',

  // Power Delivery
  'PowerDeliveryReq': 'PowerDelivery Request',
  'PowerDeliveryRes': 'PowerDelivery Response',

  // Certificate Management
  'CertificateInstallationReq': 'CertificateInstallation Request',
  'CertificateInstallationRes': 'CertificateInstallation Response',
  'CertificateUpdateReq': 'CertificateUpdate Request',
  'CertificateUpdateRes': 'CertificateUpdate Response',

  // Charging Status
  'ChargingStatusReq': 'ChargingStatus Request',
  'ChargingStatusRes': 'ChargingStatus Response',

  // Metering
  'MeteringReceiptReq': 'MeteringReceipt Request',
  'MeteringReceiptRes': 'MeteringReceipt Response',

  // Body Types
  'Body': 'Body',
  'Header': 'Header'
};

// Message direction patterns
const DIRECTION_PATTERNS = {
  evccToSecc: [
    /EVCC.*?->.*?SECC/i,
    /Send.*?(Req|Request)/i,
    /TX.*?(Req|Request)/i,
    /Sending/i,
    /->\s*SECC/i,
    /outgoing/i,
    /transmit/i,
    /EVCC.*send/i,
    /→.*SECC/i,
  ],
  seccToEvcc: [
    /SECC.*?->.*?EVCC/i,
    /Receive.*?(Res|Response)/i,
    /RX.*?(Res|Response)/i,
    /Receiving/i,
    /SECC.*?->/i,
    /incoming/i,
    /receive/i,
    /SECC.*send/i,
    /SECC.*→/i,
  ]
};

// Timestamp patterns
const TIMESTAMP_PATTERNS = [
  /(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?)/,
  /(\d{2}:\d{2}:\d{2}(?:\.\d+)?)/,
  /\[(\d{2}:\d{2}:\d{2})\]/,
  /(\d{2}:\d{2}:\d{2}\.\d{3})/,
];

/**
 * Parse a single log line to extract message information
 * @param {string} line - Log line to parse
 * @param {number} lineNumber - Line number for ordering
 * @returns {Object|null} - Parsed message or null if not a message
 */
function parseLogLine(line, lineNumber = 0) {
  if (!line || typeof line !== 'string') return null;

  const trimmedLine = line.trim();
  if (trimmedLine.length < 5) return null;

  // Try to extract timestamp
  let timestamp = null;
  for (const pattern of TIMESTAMP_PATTERNS) {
    const match = trimmedLine.match(pattern);
    if (match) {
      timestamp = match[1];
      break;
    }
  }

  // Detect message type
  let messageType = null;
  let foundType = null;

  for (const [key, value] of Object.entries(MESSAGE_TYPES)) {
    if (trimmedLine.toUpperCase().includes(key)) {
      foundType = key;
      messageType = value;
      break;
    }
  }

  // If no known message type, try to extract from common patterns
  if (!messageType) {
    const msgMatch = trimmedLine.match(/(?:Send|Receive|Sending|Receiving)\s+(\w+)/i);
    if (msgMatch) {
      messageType = msgMatch[1];
    }
  }

  // Detect direction
  let direction = null;
  let from = 'EVCC';
  let to = 'SECC';

  for (const pattern of DIRECTION_PATTERNS.evccToSecc) {
    if (pattern.test(trimmedLine)) {
      direction = 'evcc-to-secc';
      from = 'EVCC';
      to = 'SECC';
      break;
    }
  }

  if (!direction) {
    for (const pattern of DIRECTION_PATTERNS.seccToEvcc) {
      if (pattern.test(trimmedLine)) {
        direction = 'secc-to-evcc';
        from = 'SECC';
        to = 'EVCC';
        break;
      }
    }
  }

  // If still no direction, try to infer from message type
  if (!direction) {
    if (foundType && foundType.includes('Req')) {
      direction = 'evcc-to-secc';
      from = 'EVCC';
      to = 'SECC';
    } else if (foundType && foundType.includes('Res')) {
      direction = 'secc-to-evcc';
      from = 'SECC';
      to = 'EVCC';
    }
  }

  // Only return if we found meaningful information
  if (messageType || direction) {
    return {
      timestamp,
      messageType: messageType || 'Unknown Message',
      direction,
      from,
      to,
      raw: trimmedLine,
      lineNumber,
      id: `msg_${lineNumber}_${Date.now()}`
    };
  }

  return null;
}

/**
 * Parse complete log text to extract all message exchanges
 * @param {string} logText - Complete log text
 * @returns {Object} - Parsed sequence data
 */
export function parseLogToSequence(logText) {
  if (!logText) {
    return {
      messages: [],
      mermaidCode: 'sequenceDiagram\n    note over EVCC, SECC: No messages logged yet',
      stats: { total: 0, evccToSecc: 0, seccToEvcc: 0 }
    };
  }

  const lines = logText.split('\n');
  const messages = [];

  for (let i = 0; i < lines.length; i++) {
    const parsed = parseLogLine(lines[i], i);
    if (parsed) {
      messages.push(parsed);
    }
  }

  // Generate Mermaid sequence diagram code
  const mermaidCode = generateMermaidCode(messages);

  // Calculate statistics
  const stats = {
    total: messages.length,
    evccToSecc: messages.filter(m => m.direction === 'evcc-to-secc').length,
    seccToEvcc: messages.filter(m => m.direction === 'secc-to-evcc').length
  };

  return {
    messages,
    mermaidCode,
    stats
  };
}

/**
 * Generate Mermaid sequence diagram code from parsed messages
 * @param {Array} messages - Array of parsed messages
 * @returns {string} - Mermaid sequence diagram code
 */
function generateMermaidCode(messages) {
  if (!messages || messages.length === 0) {
    return 'sequenceDiagram\n    note over EVCC, SECC: No messages logged yet';
  }

  let code = 'sequenceDiagram\n';

  // Define participants
  code += '    participant EVCC\n';
  code += '    participant SECC\n';

  // Add title/note if we have messages
  if (messages.length > 0) {
    code += `\n    %% ISO 15118 Message Sequence (${messages.length} messages)\n`;
  }

  // Add messages
  for (const msg of messages) {
    // Truncate long message types and remove Mermaid-sensitive characters
    const rawLabel = msg.messageType.length > 30
      ? msg.messageType.substring(0, 27) + '...'
      : msg.messageType;
    const safeLabel = rawLabel
      .replace(/[{}\[\]:<>]/g, ' ')
      .trim();

    if (msg.direction === 'evcc-to-secc') {
      code += `    EVCC->>SECC: ${safeLabel}\n`;
    } else if (msg.direction === 'secc-to-evcc') {
      code += `    SECC->>EVCC: ${safeLabel}\n`;
    } else {
      code += `    EVCC--SECC: ${safeLabel}\n`;
    }
  }

  return code;
}

/**
 * Get message type display name
 * @param {string} typeKey - Message type key
 * @returns {string} - Display name
 */
export function getMessageTypeName(typeKey) {
  return MESSAGE_TYPES[typeKey] || typeKey;
}

/**
 * Format message for display
 * @param {Object} message - Parsed message object
 * @returns {string} - Formatted message string
 */
export function formatMessage(message) {
  if (!message) return '';

  const dir = message.direction === 'evcc-to-secc' ? '→' : '←';
  const label = `${message.from} ${dir} ${message.to}: ${message.messageType}`;

  return label;
}

export default {
  parseLogToSequence,
  parseLogLine,
  getMessageTypeName,
  formatMessage,
  MESSAGE_TYPES
};
