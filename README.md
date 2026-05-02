# PF205 Transportation System

<div align="center">

[![Java](https://img.shields.io/badge/Java_8-ED8B00?style=for-the-badge&logo=openjdk&logoColor=white)](https://openjdk.org/)
[![Swing](https://img.shields.io/badge/Java_Swing-5382A1?style=for-the-badge&logo=java&logoColor=white)](https://docs.oracle.com/javase/tutorial/uiswing/)
[![SQLite](https://img.shields.io/badge/SQLite-07405E?style=for-the-badge&logo=sqlite&logoColor=white)](https://sqlite.org/)
[![NetBeans](https://img.shields.io/badge/Apache_NetBeans-1B6AC6?style=for-the-badge&logo=apache-netbeans-ide&logoColor=white)](https://netbeans.apache.org/)

</div>

---

## Overview


**PF205 Transportation System** is a desktop academic project built with **Java Swing**. It supports everyday transportation booking workflows for **passengers (users)** and **administrators** in one application: managing routes, creating and tracking bookings, updating trip status, and viewing trip documents.

**Customers** can browse available routes, book seats, manage their bookings, and view a **boarding-pass style ticket** after booking or from their booking list.

**Administrators** can maintain routes and vehicles, manage all bookings, update booking status (including completion), view **receipts** for completed trips, and preview passenger **tickets** when needed.

Data is stored locally using **SQLite** (embedded database file created at runtime under `database/`).

---

## Features

- User registration and login with role-aware access (user vs admin)
- Route listing with search and booking flow
- Booking history for users; full booking management for admins
- Booking status workflow (e.g. Pending through Arrived / Cancelled)
- **Receipts** generated when a booking is marked **Arrived**
- **Boarding pass / ticket** UI (designed ticket layout with main panel and stub)
- Local **SQLite** persistence with automatic schema initialization

---

## Tech Stack

| Layer | Technology |
| :---- | :--------- |
| Language | Java (JDK 8) |
| UI | Java Swing, NetBeans GUI Builder (`.form` / AbsoluteLayout) |
| Database | SQLite (`sqlite-jdbc`) |
| Supporting libraries | Absolute Layout, Beans Binding, EclipseLink (project classpath) |

---

## Project Structure (high level)

- `src/Main/` — Login shell and main navigation (`Mainframe`, `MainPage`, registration flow)
- `src/UserInternalPages/` — Passenger-facing screens (transportation, bookings, settings)
- `src/AdminInternalPages/` — Admin screens (transportation, bookings, users, settings)
- `src/Configuration/` — Database connection, schema bootstrap, utilities (`ConnectionConfig`, receipts helper, etc.)
- `src/UI/` — Shared UI helpers (document dialogs, receipt/ticket panels)
- `database/` — Runtime SQLite file (`transportation.db`) created when the app connects

---

## Prerequisites

- **JDK 8** (matches project source/target `1.8`)
- **Apache NetBeans** (recommended) or another IDE that can open Ant-based Java SE projects
- JDBC driver JARs referenced by the project (e.g. **SQLite JDBC** under `src/Configuration/` as configured in NetBeans)

---

## How to Run Locally

### Option A — NetBeans

1. **Clone the repository**

   ```bash
   git clone https://github.com/your-username/your-repo-name.git
   cd your-repo-name
   ```

2. **Open project** — `File` → `Open Project…` and select this folder (NetBeans project with `nbproject/`).

3. **Resolve libraries** — Ensure classpath JARs (SQLite JDBC, etc.) exist where `nbproject/project.properties` expects them.

4. **Set main class** — Run **`Main.Mainframe`** (login window).

5. **Run** — Right-click the project → **Run**, or **F6**.

### Option B — Command line (Ant)

If **Apache Ant** is installed:

```bash
cd path/to/PF205-TransportationSystem
ant jar
java -cp "dist/TransportationSystem.jar;path/to/sqlite-jdbc.jar;..." Main.Mainframe
```

Adjust classpath separators (`;` on Windows, `:` on macOS/Linux) and include all required JARs.

---

## Database

On first run, tables such as **users**, **routes**, **bookings**, and **receipts** are created if missing (see `ConnectionConfig`). The database file path is resolved from the working directory under `database/transportation.db`.

---

## Developer

<div align="center">

<table>
  <tr>
    <td align="center">
      <img src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png" width="90" height="90" alt="Developer" />
      <br />
      <strong>Eduardo D. Masangcay</strong>
      <br />
      <em>BSIT 2-E</em>
      <br />
      <em>St. Cecilia's College-Cebu, Inc.</em>
    </td>
  </tr>
</table>

</div>

---

## Acknowledgements

- [OpenJDK](https://openjdk.org/) — Java platform  
- [SQLite](https://sqlite.org/) — embedded database  
- [Apache NetBeans](https://netbeans.apache.org/) — IDE and project tooling  
- [Shields.io](https://shields.io/) — README badges  

---

## License

This project is developed for academic purposes at **St. Cecilia's College-Cebu, Inc.**

© 2026 Eduardo D. Masangcay — BSIT 2-E
