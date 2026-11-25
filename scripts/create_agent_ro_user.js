// Creates the read-only MongoDB user for the MCP agent tools.
//
// Usage (against the production mongo container):
//   docker exec -i mongo mongosh --quiet -u <admin> -p <pass> --authenticationDatabase admin \
//     < scripts/create_agent_ro_user.js
//
// Or from inside the mongo container:
//   mongosh --quiet -u <admin> -p <pass> --authenticationDatabase admin /scripts/create_agent_ro_user.js
//
// Set AGENT_RO_PASSWORD in the environment before running (generated, stored
// in the deployment .env as AGENT_MONGODB_URL).

const user = "agent_ro";
const pass = process.env.AGENT_RO_PASSWORD;
if (!pass) {
  throw new Error("AGENT_RO_PASSWORD env var must be set");
}

const db = db.getSiblingDB("congiuntura");

// Drop + recreate so re-running rotates the password deterministically.
db.runCommand({ dropUser: user });

db.createUser({
  user: user,
  pwd: pass,
  roles: [{ role: "read", db: "congiuntura" }],
});

print("Created user 'agent_ro' with read-only access to 'congiuntura'.");
print(
  "Set in the app environment: AGENT_MONGODB_URL=mongodb://agent_ro:<pass>@mongo:27017/congiuntura?authSource=congiuntura"
);
