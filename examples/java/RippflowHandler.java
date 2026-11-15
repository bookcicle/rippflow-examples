import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class RippflowHandler {
  private static String extractString(String json, String key, String fallback) {
    Pattern pattern = Pattern.compile("\"" + Pattern.quote(key) + "\"\\s*:\\s*\"([^\"]*)\"");
    Matcher matcher = pattern.matcher(json);
    return matcher.find() ? matcher.group(1) : fallback;
  }

  private static int extractInt(String json, String key, int fallback) {
    Pattern pattern = Pattern.compile("\"" + Pattern.quote(key) + "\"\\s*:\\s*([0-9]+)");
    Matcher matcher = pattern.matcher(json);
    return matcher.find() ? Integer.parseInt(matcher.group(1)) : fallback;
  }

  private static String jsonEscape(String value) {
    return value
        .replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t");
  }

  private static String sha256Hex(String value) {
    try {
      MessageDigest md = MessageDigest.getInstance("SHA-256");
      byte[] digest = md.digest(value.getBytes(StandardCharsets.UTF_8));
      StringBuilder hex = new StringBuilder(digest.length * 2);
      for (byte b : digest) {
        hex.append(String.format("%02x", b));
      }
      return hex.toString();
    } catch (NoSuchAlgorithmException e) {
      throw new RuntimeException(e);
    }
  }

  public static void main(String[] args) throws IOException {
    String raw = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
    if (raw.isBlank()) {
      System.err.println("empty handler stdin");
      System.exit(1);
    }

    String phase = extractString(raw, "phase", "unknown");
    String requestId = extractString(raw, "requestId", "local-run");
    String shard = extractString(raw, "shard", "default");
    String service = extractString(raw, "service", "java-example-handler");
    int loopCount = extractInt(raw, "loopCount", 0);

    if ("fatal-crash.bin".equals(shard) && "process-item".equals(phase)) {
      System.err.println(
          "SIMULATED_FATAL_CRASH: unrecoverable Java worker fault for shard 'fatal-crash.bin'");
      System.exit(1);
    }

    String outcome = "completed";
    String reasonJson = "null";

    if ("verify-balance".equals(phase) && "stmt-feb.pdf".equals(shard) && loopCount == 0) {
      outcome = "rework";
      reasonJson = "\"balance_mismatch_low_dpi\"";
    } else if ("loudness-qa".equals(phase) && "scene-02.mp4".equals(shard) && loopCount == 0) {
      outcome = "rework";
      reasonJson = "\"loudness_outside_target\"";
    } else if ("audit-item".equals(phase) && "corrupt-item.bin".equals(shard)) {
      outcome = "rework";
      reasonJson = "\"persistent_checksum_corruption\"";
    } else if ("audit-item".equals(phase) && "recoverable-item.bin".equals(shard) && loopCount == 0) {
      outcome = "rework";
      reasonJson = "\"transient_checksum_drift\"";
    }

    String digest = sha256Hex(requestId + ":" + phase + ":" + shard + ":" + loopCount);
    String outputPointer =
        "s3://rippflow-examples/" + requestId + "/" + phase + "-" + shard + "-loop" + loopCount + ".json";

    String response =
        "{\"outcome\":\""
            + outcome
            + "\",\"outputPointer\":\""
            + jsonEscape(outputPointer)
            + "\",\"payload\":{\"simulated\":true,\"runtime\":\"java\",\"service\":\""
            + jsonEscape(service)
            + "\",\"phase\":\""
            + jsonEscape(phase)
            + "\",\"shard\":\""
            + jsonEscape(shard)
            + "\",\"loopCount\":"
            + loopCount
            + ",\"reason\":"
            + reasonJson
            + ",\"sha256\":\""
            + digest
            + "\"}}";

    System.out.println(response);
  }
}
